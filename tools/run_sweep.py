#!/usr/bin/env python3
"""Drive one load sweep and record what happened, so it can be read back later.

This tool measures nothing and analyses nothing. Its entire job is to make the
declared interval edges a RECORDED FACT rather than something reconstructed from
memory afterwards, because a post-hoc read of the archive has nothing to align to
otherwise. It writes one JSONL record per load level, pass or fail, plus a session
record holding the seed and the declarations.

Three things it refuses to do, each encoded here rather than left to the operator:

  1. run with a measurement interval below the floor implied by the MEASURED
     cadence (--cadence-json, from measure_cadence.py);
  2. run with a split sub-interval length that aliases against that cadence;
  3. run at all if a reference metric is missing from the scrape path it will
     later be read from.

Usage:
    # rehearse the whole schedule in seconds, touching nothing
    python3 -I run_sweep.py --node node2 --dry-run --cadence-json cadence.json \
        --rates 0,1000,8000,16000,32000,48000 --out /tmp/sweep

    # for real, driving a generator
    python3 -I run_sweep.py --node node2 --cadence-json cadence.json \
        --rates 0,1000,8000,16000,32000,48000 --t-s 90 --t-m 300 --t-d 60 \
        --gen-cmd 'iperf3 -c 10.42.0.9 -u -b {rate}pps -t {duration}' \
        --out /run/archive/session-01
"""

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promsamples  # noqa: E402
import runmeta  # noqa: E402

# Reference metrics that must be non-empty before a sweep may start. Probed
# THROUGH the same path the analysis will read from, because a metric that exists
# on the exporter but never reaches Prometheus is absent for every purpose that
# matters here.
REQUIRED_REFERENCE = [
    ("hardware_rapl_energy_joules_total", "node",
     "every energy result, and therefore `b` in watts and all energy-per-unit-of-work"),
    ("hardware_cpu_seconds_total", "node",
     "every CPU-time result, and the comparison of energy shape against CPU shape"),
    ("hardware_cgroup_root_cpu_seconds_total", "node",
     "whether node CPU time is accounted for by the control-group hierarchy"),
]

# Active idle appears as >=3 positions in the shuffled order rather than as a
# bracket around the run. Three is the minimum at which floor drift becomes a
# separately estimable term instead of a bias on the intercept; the cost of a
# fourth is one level of wall-clock, so raise it if the schedule allows.
MIN_ACTIVE_IDLE_LEVELS = 3


def preflight(url, node):
    """Gate 3 in its smallest honest form: is each reference series actually there?"""
    results, fatal = [], False
    for metric, label, claim in REQUIRED_REFERENCE:
        expr = f'{metric}{{{label}="{node}"}}'
        try:
            res = promsamples.instant(expr, url=url)["result"]
        except Exception as exc:
            results.append((metric, f"QUERY FAILED: {exc}", claim))
            fatal = True
            continue
        if not res:
            results.append((metric, "ABSENT", claim))
            fatal = True
        else:
            results.append((metric, f"ok ({len(res)} series)", claim))
    return results, fatal


def check_interval(cadence_json, t_m, split_halves=2):
    """Refuse a t_m the measured cadence cannot support. Returns (ok, notes)."""
    if not cadence_json:
        return True, ["no --cadence-json given: t_m NOT checked against a measured "
                      "cadence. Do not use this run for a published number."]
    with open(cadence_json) as fh:
        cad = json.load(fh)
    d = cad["derivation"]
    notes, ok = [], True
    notes.append(f"measured cadence {d['period_s']:.2f} s "
                 f"(slowest reference metric: {d['slowest_reference_metric']})")
    if t_m < d["t_m_hard_floor_s"]:
        notes.append(f"REFUSED: t_m={t_m}s is below the hard floor "
                     f"{d['t_m_hard_floor_s']}s -- a split-half test is impossible")
        ok = False
    elif t_m < d["t_m_usable_floor_s"]:
        notes.append(f"REFUSED: t_m={t_m}s is below the usable floor "
                     f"{d['t_m_usable_floor_s']}s "
                     f"({d['samples_per_half_usable']} samples per half)")
        ok = False
    else:
        notes.append(f"t_m={t_m}s clears the usable floor {d['t_m_usable_floor_s']}s")

    # The split-half test cuts t_m into `split_halves` pieces; that piece length
    # must not alias against the grid.
    piece = t_m / split_halves
    k = piece / d["period_s"]
    if abs(k - round(k)) > d["alias_tolerance"]:
        notes.append(f"REFUSED: split piece {piece:.1f}s is {k:.2f} cadence periods, "
                     f"which aliases (tolerance {d['alias_tolerance']})")
        ok = False
    else:
        notes.append(f"split piece {piece:.1f}s = {round(k)} cadence periods exactly")
    return ok, notes


def build_order(rates, n_idle, seed):
    """Seeded shuffle of nonzero rates plus n_idle active-idle levels."""
    nonzero = [r for r in rates if r > 0]
    levels = [{"lambda": r, "kind": "load"} for r in nonzero]
    levels += [{"lambda": 0.0, "kind": "active-idle"} for _ in range(n_idle)]
    rng = random.Random(seed)
    rng.shuffle(levels)
    for i, lv in enumerate(levels):
        lv["position"] = i
    return levels


def run_level(level, t_s, t_m, t_d, gen_cmd, outdir, dry, scale,
              ids=None, ann=None, lambda_unit=""):
    rec = dict(ids or {})
    rec.update(level)
    rec["t_set"] = time.time()
    rec["t_set_iso"] = runmeta.iso(rec["t_set"])
    proc = None
    duration = int((t_s + t_m) * scale) or 1
    tag = "active-idle" if level["kind"] == "active-idle" else f"{level['lambda']:.0f}{lambda_unit}"

    if level["lambda"] > 0 and gen_cmd and not dry:
        cmd = gen_cmd.format(rate=int(level["lambda"]), duration=duration)
        logp = os.path.join(outdir, f"gen-{level['position']:02d}.log")
        rec["generator_cmd"] = cmd
        rec["generator_log"] = logp
        with open(logp, "w") as lf:
            proc = subprocess.Popen(shlex.split(cmd), stdout=lf, stderr=subprocess.STDOUT)
    elif level["lambda"] > 0:
        rec["generator_cmd"] = (gen_cmd or "").format(rate=int(level["lambda"]),
                                                      duration=duration) or None
        rec["generator_skipped"] = "dry-run" if dry else "no --gen-cmd"

    time.sleep(t_s * scale)
    rec["t_m_start"] = time.time()          # the declared opening edge
    rec["t_m_start_iso"] = runmeta.iso(rec["t_m_start"])
    time.sleep(t_m * scale)
    rec["t_m_end"] = time.time()            # the declared closing edge
    rec["t_m_end_iso"] = runmeta.iso(rec["t_m_end"])
    rec["t_m_elapsed_s"] = rec["t_m_end"] - rec["t_m_start"]

    # Annotate the MEASUREMENT INTERVAL only, not the whole level: on a dashboard
    # the shaded region is then exactly the span a number will be taken from, and
    # the settling and drain periods stay visibly outside it.
    if ann and ann.enabled:
        ann.annotate(
            text=f"{rec.get('run_id', 'run')} level {level['position']}: {tag}",
            tags=["load-sweep", f"experiment:{rec.get('experiment_id', '-')}",
                  f"run:{rec.get('run_id', '-')}",
                  "active-idle" if level["kind"] == "active-idle" else "load"],
            start=rec["t_m_start"], end=rec["t_m_end"])

    if proc:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        rec["generator_returncode"] = proc.returncode
    rec["t_drain_start"] = time.time()
    rec["t_drain_start_iso"] = runmeta.iso(rec["t_drain_start"])
    time.sleep(t_d * scale)
    rec["t_drain_end"] = time.time()
    rec["t_drain_end_iso"] = runmeta.iso(rec["t_drain_end"])
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=promsamples.DEFAULT_URL)
    ap.add_argument("--node", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--experiment-id", required=True,
                    help="the campaign or configuration. Results from different "
                         "experiment-ids are never pooled, so name it for what is "
                         "held fixed, e.g. 'cdn-baseline' or 'cdn-4k-contexts'.")
    ap.add_argument("--grafana-url", default=os.environ.get("GRAFANA_URL"),
                    help="optional; annotate each measurement interval for live supervision")
    ap.add_argument("--grafana-token", default=os.environ.get("GRAFANA_TOKEN"))
    ap.add_argument("--grafana-dashboard-uid", default=os.environ.get("GRAFANA_DASHBOARD_UID"))
    ap.add_argument("--rates", required=True,
                    help="comma-separated offered rates; 0 is ignored here, use --idle-levels")
    ap.add_argument("--lambda-unit", default="pps",
                    help="unit of the offered rate. Travels with every slope derived from it.")
    ap.add_argument("--idle-levels", type=int, default=MIN_ACTIVE_IDLE_LEVELS)
    ap.add_argument("--t-s", type=float, default=90.0, help="pre-measurement period")
    ap.add_argument("--t-m", type=float, default=300.0, help="measurement interval")
    ap.add_argument("--t-d", type=float, default=60.0, help="post-measurement period")
    ap.add_argument("--seed", type=int, default=None, help="default: derived from the clock and recorded")
    ap.add_argument("--cadence-json", default=None, help="output of measure_cadence.py")
    ap.add_argument("--gen-cmd", default=None,
                    help="generator command; {rate} and {duration} are substituted")
    ap.add_argument("--dry-run", action="store_true",
                    help="rehearse the schedule with periods scaled to --dry-scale")
    ap.add_argument("--dry-scale", type=float, default=0.01)
    ap.add_argument("--force", action="store_true",
                    help="run even if a refusal fired. Recorded in the session file.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    seed = args.seed if args.seed is not None else int(time.time())
    rates = [float(x) for x in args.rates.split(",")]
    scale = args.dry_scale if args.dry_run else 1.0
    experiment_id = runmeta.slug(args.experiment_id)
    run_id = runmeta.make_run_id(experiment_id, seed)
    ids = {"experiment_id": experiment_id, "run_id": run_id}
    ann = runmeta.Annotator(args.grafana_url, args.grafana_token,
                            args.grafana_dashboard_uid)

    if args.idle_levels < MIN_ACTIVE_IDLE_LEVELS:
        print(f"REFUSED: --idle-levels={args.idle_levels} is below {MIN_ACTIVE_IDLE_LEVELS}; "
              "with fewer than three shuffled active-idle levels, floor drift cannot be "
              "separated from the intercept and the intercept becomes a subtracted baseline.",
              file=sys.stderr)
        if not args.force:
            return 2

    print("preflight -- reference metrics through the scraper's own path")
    probes, fatal = preflight(args.url, args.node)
    for metric, status, claim in probes:
        print(f"  {metric:<45} {status}")
        if status != "ok" and not status.startswith("ok"):
            print(f"      claim that dies: {claim}")

    print("\npreflight -- measurement interval against the measured cadence")
    iv_ok, notes = check_interval(args.cadence_json, args.t_m)
    for n in notes:
        print(f"  {n}")

    refused = fatal or not iv_ok
    if refused and not args.force:
        print("\nREFUSED. Fix the above, or pass --force to record a run as non-conforming.",
              file=sys.stderr)
        return 2

    levels = build_order(rates, args.idle_levels, seed)
    total = len(levels) * (args.t_s + args.t_m + args.t_d)
    print(f"\n{len(levels)} levels, seed={seed}, "
          f"{total:.0f}s nominal ({total / 60:.1f} min)"
          + (f"  [DRY RUN at {scale}x = {total * scale:.0f}s]" if args.dry_run else ""))
    print("  order: " + " ".join(
        ("IDLE" if lv["kind"] == "active-idle" else f"{lv['lambda']:.0f}") for lv in levels))

    session = {
        "schema_version": 2,
        "experiment_id": experiment_id,
        "run_id": run_id,
        "timestamps_note": runmeta.ISO_NOTE,
        "node": args.node,
        "seed": seed,
        "lambda_unit": args.lambda_unit,
        "periods_s": {"t_s": args.t_s, "t_m": args.t_m, "t_d": args.t_d},
        "idle_levels": args.idle_levels,
        "rates": rates,
        "dry_run": args.dry_run,
        "dry_scale": scale if args.dry_run else None,
        "preflight": [{"metric": m, "status": s, "claim_if_absent": c} for m, s, c in probes],
        "interval_check": notes,
        "refused_but_forced": bool(refused and args.force),
        "cadence_json": os.path.abspath(args.cadence_json) if args.cadence_json else None,
        "generator_cmd_template": args.gen_cmd,
        "grafana_annotations": "enabled" if ann.enabled else "not configured",
        "started": time.time(),
    }
    session["started_iso"] = runmeta.iso(session["started"])
    spath = os.path.join(args.out, "session.json")
    lpath = os.path.join(args.out, "levels.jsonl")

    with open(lpath, "w") as lf:
        for lv in levels:
            tag = "IDLE" if lv["kind"] == "active-idle" else f"{lv['lambda']:.0f} {args.lambda_unit}"
            print(f"  [{lv['position'] + 1}/{len(levels)}] {tag}", flush=True)
            rec = run_level(lv, args.t_s, args.t_m, args.t_d,
                            args.gen_cmd, args.out, args.dry_run, scale,
                            ids=ids, ann=ann, lambda_unit=args.lambda_unit)
            lf.write(json.dumps(rec) + "\n")
            lf.flush()

    session["finished"] = time.time()
    session["finished_iso"] = runmeta.iso(session["finished"])
    # The archive must cover the whole session INCLUDING the periods that produce
    # no numbers, because the settling and carry-over checks are computed from them.
    # The margin is one post-measurement period at each end, not a fixed 60 s, so
    # that it scales with the decay the periods were derived from.
    margin = max(args.t_d, 60.0)
    session["archive_window"] = {
        "start": session["started"] - margin,
        "end": session["finished"] + margin,
        "start_iso": runmeta.iso(session["started"] - margin),
        "end_iso": runmeta.iso(session["finished"] + margin),
        "margin_s": margin,
    }
    if ann.enabled:
        ann.annotate(text=f"{run_id}: session",
                     tags=["load-sweep", f"experiment:{experiment_id}",
                           f"run:{run_id}", "session"],
                     start=session["started"], end=session["finished"])
        session["grafana_annotations_posted"] = ann.posted
        session["grafana_annotations_failed"] = ann.failures
    with open(spath, "w") as fh:
        json.dump(session, fh, indent=2)

    print(f"\nexperiment={experiment_id}  run={run_id}")
    print(f"wrote {spath}\n      {lpath}")
    if ann.enabled:
        print(f"      {ann.posted} Grafana annotations posted, {ann.failures} failed")
    print("\nnext, while the samples are still inside retention:")
    print(f"  python3 -I archive_samples.py --session {spath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
