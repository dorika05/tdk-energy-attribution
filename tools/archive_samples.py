#!/usr/bin/env python3
"""Archive raw per-sample metric data to disk, losslessly, with a presence manifest.

This exists because Prometheus retention expires (45 days OR 45 GB on a 10 Gi
volume here, so the volume fills first) and because a result read live can never
be recomputed over different interval boundaries afterwards. The archive is the
only artifact from which the validity conditions can be re-examined, so it is
written BEFORE a campaign is trusted, not after it is finished.

The archive is RAW. Nothing is corrected, unwrapped, resampled or aligned on the
way in. Wraps and gaps are DETECTED and recorded in the manifest, and left in the
data, so that the archive and the analysis cannot silently disagree about what was
measured. One definition site for the corrections lives in the analysis, not here.

Usage:
    python3 -I archive_samples.py --node node2 --hours 1 --out /path/to/archive
    python3 -I archive_samples.py --node node2 --start 1791370000 --end 1791373600 \
        --out /path/to/archive --label session-01
"""

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promsamples  # noqa: E402
import runmeta  # noqa: E402

# Everything the analysis consumes. Each entry DECLARES four things, and every
# check below is driven by the declaration rather than guessed from the name:
#
#   role   "reference" may supply a reference value; "under-test" is archived for
#          completeness and may never supply one; "witness" supports a mechanism
#          claim without entering a fit.
#   node   the label carrying node identity FOR THAT EXPORTER. These disagree:
#          hardware-exporter and Scaphandre use `node`, Kepler uses `node_name`,
#          node-exporter and cAdvisor carry none. Assuming `node` everywhere
#          returns zero series, which then reads as "metric absent".
#   kind   "counter" (monotone; a decrease is a reset or a wrap and is reported)
#          or "gauge" (a decrease is ordinary and must NOT be counted as a reset).
#          Verified on this cluster: hardware_cpu_frequency_average_hertz is a
#          gauge and fell 174 times in one hour, which a name-based counter
#          heuristic would have reported as 174 counter resets.
#   modes  the mode values that must be PRESENT, or None if the metric has no
#          mode label. The expected set differs per metric and cannot be shared:
#          /proc/stat carries eight modes, while cgroup-v2 cpu.stat carries only
#          usage/user/system, so one shared list makes the cgroup leg look broken.
#          `nice` is required on the /proc/stat leg because a missing `nice` was
#          once worth 0.038-0.052 cores -- 52% of a control configuration's whole
#          measured quantity -- while reading as a confident 0.000.
PROC_MODES = ["user", "nice", "system", "irq", "softirq", "iowait", "steal", "idle"]
CGROUP_MODES = ["usage", "user", "system"]

METRICS = [
    dict(role="reference",  metric="hardware_rapl_energy_joules_total",                node="node",      kind="counter", modes=None),
    dict(role="reference",  metric="hardware_cgroup_root_cpu_seconds_total",           node="node",      kind="counter", modes=CGROUP_MODES),
    dict(role="reference",  metric="hardware_cpu_seconds_total",                       node="node",      kind="counter", modes=PROC_MODES),
    dict(role="reference",  metric="hardware_cpu_core_seconds_total",                  node="node",      kind="counter", modes=PROC_MODES),
    dict(role="witness",    metric="hardware_cpu_cstate_residency_microseconds_total", node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_cpu_frequency_average_hertz",             node="node",      kind="gauge",   modes=None),
    dict(role="witness",    metric="hardware_cpu_thermal_throttle_total",              node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_softnet_squeezed_total",                  node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_kthread_cpu_seconds_total",               node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_irq_count_total",                         node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_network_receive_packets_total",           node="node",      kind="counter", modes=None),
    dict(role="witness",    metric="hardware_network_transmit_packets_total",          node="node",      kind="counter", modes=None),
    dict(role="under-test", metric="container_cpu_usage_seconds_total",                node="node",      kind="counter", modes=None),
    dict(role="under-test", metric="kepler_node_cpu_joules_total",                     node="node_name", kind="counter", modes=None),
    dict(role="under-test", metric="kepler_pod_cpu_joules_total",                      node="node_name", kind="counter", modes=None),
    dict(role="under-test", metric="scaph_host_energy_microjoules",                    node="node",      kind="counter", modes=None),
]

# Observed on this cluster 2026-10-07: the package RAPL counter range is 2^18 J.
# Measured as max(observed drop) + (energy accumulated during that interval),
# over a 24 h scan of raw samples on node1-node4; every drop fell in
# 262053.5-262057.0 J against a per-interval accumulation of ~86 J.
# At idle (~8.6-9.0 W) this wraps every ~8.1-8.4 h, i.e. 2-3 times a day on every
# node, and proportionally faster under load. A wrap is therefore a ROUTINE event
# in any multi-hour session, not an exceptional one.
RAPL_WRAP_RANGE_JOULES = 262144.0


def archive(url, node, start, end, outdir, label, experiment_id, run_id, clamped=None):
    os.makedirs(outdir, exist_ok=True)
    stamp = label or run_id or runmeta.iso(start).replace(":", "")
    csv_path = os.path.join(outdir, f"samples-{stamp}.csv")
    man_path = os.path.join(outdir, f"manifest-{stamp}.json")
    now = time.time()

    manifest = {
        "schema_version": 2,
        "experiment_id": experiment_id,
        "run_id": run_id,
        "timestamps_note": runmeta.ISO_NOTE,
        "archived_at": now,
        "archived_at_iso": runmeta.iso(now),
        "prometheus": url,
        "node": node,
        "window": {"start": start, "end": end, "span_s": end - start,
                   "start_iso": runmeta.iso(start), "end_iso": runmeta.iso(end)},
        "window_clamped_to_now": clamped,
        "wrap_range_joules_assumed_for_detection": RAPL_WRAP_RANGE_JOULES,
        "metrics": [],
    }

    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        # Long format, one row per sample. `labels` is the full label set as JSON so
        # the archive is lossless regardless of which exporter's labels appear;
        # `node` is lifted out because every query filters on it.
        #
        # experiment_id and run_id are on EVERY row, not just in the manifest. That
        # is what makes "results from different configurations are not pooled" a
        # filter the analysis applies rather than a rule someone has to remember
        # while concatenating files.
        #
        # `timestamp` (epoch) is authoritative; `datetime` is UTC with an explicit
        # offset and exists for humans. Never compute from the string.
        w.writerow(["experiment_id", "run_id", "timestamp", "datetime",
                    "metric", "role", "node", "labels", "value"])

        for spec in METRICS:
            role, metric = spec["role"], spec["metric"]
            node_label, kind, want_modes = spec["node"], spec["kind"], spec["modes"]
            sel = f'{metric}{{{node_label}="{node}"}}' if node_label else metric
            entry = {"metric": metric, "role": role, "kind": kind, "selector": sel}
            try:
                series = promsamples.samples(sel, start, end, url=url)
            except Exception as exc:
                entry["status"] = f"QUERY FAILED: {exc}"
                manifest["metrics"].append(entry)
                print(f"  {metric:<50} QUERY FAILED {exc}")
                continue

            if not series:
                # Distinguish "not exported" from "selector matched nothing", because
                # collapsing them is how an absent series becomes a zero.
                try:
                    whole = promsamples.instant(metric, url=url)["result"]
                except Exception:
                    whole = []
                if whole:
                    keys = sorted(set(k for s in whole for k in s["metric"]) - {"__name__"})
                    entry["status"] = "SELECTOR MISMATCH"
                    entry["series_unfiltered"] = len(whole)
                    entry["labels_present"] = keys
                else:
                    entry["status"] = "ABSENT: not exported"
                manifest["metrics"].append(entry)
                print(f"  {metric:<50} {entry['status']}")
                continue

            rows = 0
            cadences, wraps, gaps = [], [], []
            zero_modes, seen_modes = [], set()
            for slot in series.values():
                labels, points = slot["labels"], slot["points"]
                mode = labels.get("mode")
                if mode:
                    seen_modes.add(mode)
                total = 0.0
                for i, (ts, val) in enumerate(points):
                    w.writerow([experiment_id, run_id, repr(ts), runmeta.iso(ts),
                                metric, role, node, json.dumps(labels, sort_keys=True), val])
                    rows += 1
                    if i:
                        dt = ts - points[i - 1][0]
                        cadences.append(dt)
                        a, b = float(points[i - 1][1]), float(val)
                        if b < a:
                            # Only meaningful for a declared counter. On a gauge a
                            # decrease is the normal case and counting it as a reset
                            # manufactures hundreds of phantom faults per hour.
                            if kind == "counter":
                                wraps.append({"labels": labels, "at": ts, "drop": a - b,
                                              "looks_like_wrap":
                                                  abs((a - b) - RAPL_WRAP_RANGE_JOULES)
                                                  < 0.01 * RAPL_WRAP_RANGE_JOULES})
                        else:
                            total += b - a
                if want_modes and mode and total == 0.0 and mode in want_modes:
                    zero_modes.append(mode)

            cadences.sort()
            med = cadences[len(cadences) // 2] if cadences else None
            # Expected sample count from the MEASURED cadence of this metric, never
            # from a configured scrape_interval and never from another metric's.
            expected = int((end - start) / med) if med else None
            if med:
                for i, dt in enumerate(cadences):
                    if dt > 2.5 * med:
                        gaps.append(round(dt, 2))

            entry.update({
                "status": "ok",
                "series": len(series),
                "rows": rows,
                "median_cadence_s": round(med, 3) if med else None,
                "expected_samples_per_series": expected,
                "observed_samples_per_series": round(rows / len(series), 1),
                "presence_fraction": round((rows / len(series)) / expected, 4) if expected else None,
                "gaps_over_2_5x_cadence": sorted(set(gaps))[:10],
                "counter_decreases": len(wraps),
                "decreases_consistent_with_rapl_wrap": sum(1 for x in wraps if x["looks_like_wrap"]),
                "modes_expected": want_modes,
                "modes_seen": sorted(seen_modes) or None,
                "modes_expected_missing": (sorted(set(want_modes) - seen_modes) or None)
                                          if want_modes else None,
                "suspicious_zero_modes": sorted(set(zero_modes)) or None,
            })
            manifest["metrics"].append(entry)
            print(f"  {metric:<50} {len(series):>4} series  {rows:>7} rows  "
                  f"P={med:.2f}s  present={entry['presence_fraction']}  "
                  f"decreases={len(wraps)}"
                  + (f"  MISSING MODES {entry['modes_expected_missing']}"
                     if entry["modes_expected_missing"] else ""))

    with open(man_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    return csv_path, man_path, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=promsamples.DEFAULT_URL)
    ap.add_argument("--session", default=None,
                    help="session.json from run_sweep.py: takes the node, the ids and "
                         "the archive window from it, so none of them is retyped")
    ap.add_argument("--node", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--experiment-id", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--label", default=None, help="name this archive; defaults to the run id")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--end", type=float, default=None)
    args = ap.parse_args()

    node, out = args.node, args.out
    experiment_id, run_id = args.experiment_id, args.run_id
    start = end = None

    if args.session:
        with open(args.session) as fh:
            sess = json.load(fh)
        node = node or sess["node"]
        out = out or os.path.dirname(os.path.abspath(args.session))
        experiment_id = experiment_id or sess.get("experiment_id")
        run_id = run_id or sess.get("run_id")
        win = sess.get("archive_window") or {}
        start, end = win.get("start"), win.get("end")
        if start is None or end is None:
            ap.error(f"{args.session} has no archive_window; the sweep may not have finished")

    if args.hours is not None:
        end = args.end or time.time()
        start = end - args.hours * 3600
    elif args.start and args.end:
        start, end = args.start, args.end

    if start is None or end is None:
        ap.error("give --session, or --hours, or both --start and --end")

    # The archive window carries a trailing margin past the end of the sweep, so
    # archiving the moment a sweep finishes asks for samples that have not been
    # scraped yet. Those missing samples are then counted against presence, and a
    # perfectly good archive reports ~64% present. Clamp to now, and SAY that the
    # window was clamped, so a thin tail is visible as "come back later" rather
    # than read as a gap in the data.
    now = time.time()
    clamped = None
    if end > now:
        clamped = {"requested_end": end, "requested_end_iso": runmeta.iso(end),
                   "clamped_to": now, "clamped_to_iso": runmeta.iso(now),
                   "short_by_s": round(end - now, 1)}
        print(f"  NOTE: window ends {end - now:.0f}s in the future; clamped to now. "
              f"Re-run in {end - now:.0f}s to capture the trailing margin.")
        end = now
    if not node or not out:
        ap.error("--node and --out are required unless --session supplies them")
    # An archive with no run id can still be written -- a one-off probe is a real
    # use -- but it is labelled so, rather than silently acquiring a blank column.
    experiment_id = experiment_id or "unassigned"
    run_id = run_id or "unassigned"

    print(f"archiving node={node} {end - start:.0f}s -> {out}")
    print(f"  experiment={experiment_id}  run={run_id}")
    print(f"  window {runmeta.iso(start)} .. {runmeta.iso(end)}")
    csv_path, man_path, man = archive(args.url, node, start, end, out,
                                      args.label, experiment_id, run_id, clamped)

    ok = [m for m in man["metrics"] if m.get("status") == "ok"]
    bad = [m for m in man["metrics"] if m.get("status") != "ok"]
    thin = [m for m in ok if (m["presence_fraction"] or 1) < 0.98]
    print(f"\n{len(ok)} metrics archived, {len(bad)} not resolved")
    if thin:
        print("  below 98% presence against their own measured cadence:")
        for m in thin:
            print(f"    {m['metric']}  {m['presence_fraction']}  gaps {m['gaps_over_2_5x_cadence']}")
    print(f"  {csv_path}\n  {man_path}")
    print(f"  {os.path.getsize(csv_path) / 1e6:.1f} MB for {end - start:.0f}s of one node")
    return 0


if __name__ == "__main__":
    sys.exit(main())
