#!/usr/bin/env python3
"""Is the infrastructure ready for a measurement campaign? One command, one verdict.

This is the DevOps-to-researcher handover check. It verifies the prerequisites that
are somebody else's responsibility -- cluster reachable, node eligible, every
reference metric flowing, cadence measurable, storage headroom, supervision wired
-- and then states plainly what it does NOT provide, because those items are the
researcher's own contribution and must not look like infrastructure gaps.

Exit codes:
    0   READY       every prerequisite passed
    1   DEGRADED    everything required passed, something optional did not
    2   BLOCKED     a required prerequisite failed; a campaign would be invalid

Run it after any cluster change, and keep its output beside the session it
preceded. It prints measured values, never inherited ones.

Usage:
    python3 -I infra_status.py --node node2
    python3 -I infra_status.py --node node2 --json status.json
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_cadence  # noqa: E402
import promsamples  # noqa: E402
import runmeta  # noqa: E402

# --- What the infrastructure must supply, and the claim each one carries -------
#
# Every entry is REQUIRED unless marked optional. A required failure is BLOCKED
# rather than a warning, because a campaign run without it produces numbers that
# have to be thrown away -- which is more expensive than not running.
REQUIRED_SERIES = [
    ("hardware_rapl_energy_joules_total", "node", 2,
     "all energy results: the intercept in watts, the slope in watts per unit load, "
     "and every energy-per-unit-of-work figure"),
    ("hardware_cpu_seconds_total", "node", 8,
     "all CPU-time results, and the comparison of energy shape against CPU shape"),
    ("hardware_cgroup_root_cpu_seconds_total", "node", 3,
     "the cross-check between two reference instruments on the same quantity"),
    ("hardware_cpu_core_seconds_total", "node", 16,
     "per-core placement, and therefore any statement about where softirq work lands"),
]

OPTIONAL_SERIES = [
    ("hardware_cpu_cstate_residency_microseconds_total", "node", 1,
     "the idle-state witness for the energy-shape mechanism (RQ2's proposed cause)"),
    ("hardware_cpu_frequency_average_hertz", "node", 1,
     "seeing when a settling transient has finished"),
    ("hardware_softnet_squeezed_total", "node", 1,
     "the NAPI-budget-exhaustion witness"),
    ("hardware_cpu_thermal_throttle_total", "node", 1,
     "detecting a level invalidated by thermal throttling"),
    ("hardware_network_receive_packets_total", "node", 1,
     "the achieved-load witness, i.e. evidence the generator is not the bottleneck"),
]

# /proc/stat carries eight modes on Linux. All eight must be PRESENT as series --
# a mode that never arrives is summed as a zero by any consumer that iterates over
# what it found. `nice` is the one that actually went missing in the reference
# study, and it was worth 52% of a control configuration's measured quantity.
REQUIRED_PROC_MODES = ["user", "nice", "system", "irq", "softirq", "iowait", "steal", "idle"]

# Below this, presence is reported as a failure rather than a note. 0.98 allows a
# single missed scrape in an hour at a 10 s cadence (360 samples) and little more.
MIN_PRESENCE = 0.98

# Prometheus must retain samples for longer than the gap between measuring and
# archiving. 2 h is the smallest horizon in which a forgotten archive is still
# recoverable the same working day; it is not a statement about the campaign.
MIN_RETENTION_HOURS = 2.0

# What the researcher must derive, and must NOT inherit. Printed every run so the
# handover boundary is impossible to miss. Each is a value the method requires be
# re-derived wherever it runs.
RESEARCHER_MUST_DERIVE = [
    ("t_m, the measurement interval",
     "choose it above the measured lower bound printed above, and justify the choice on "
     "noise and wall-clock -- the cadence bounds it, it does not set it"),
    ("t_s and t_d, the pre- and post-measurement periods",
     "from a measured step response on YOUR application: the rise for t_s, the "
     "decay to a stated residual for t_d. 90 s / 60 s are another cluster's numbers"),
    ("the limit of detection",
     "from the between-session spread of active idle on THIS node, across repeat "
     "sessions on different days. Not the between-blade spread, which is larger "
     "and measures something else"),
    ("lambda and its unit",
     "what is actually settable at the generator, stated once and carried with "
     "every slope. pps for UDP; a paced byte rate for TCP"),
    ("the load levels",
     "their number and spacing. The span of lambda drives the interval on the "
     "slope. Do not inherit another study's rates or its mechanism onset"),
]

# What this infrastructure deliberately does not do. Listed so an absence is read
# as a boundary rather than as a bug to be filed against the tooling.
NOT_PROVIDED = [
    "any analysis: no counter differencing, no unwrapping, no per-level validity "
    "verdicts, no fitting, no selection criterion. This is the researcher's work",
    "the application under measurement, and the load generator",
    "per-container energy -- one energy counter exists per processor package, and "
    "no per-container counter exists at any price",
    "server-level power -- no inlet meter exists on this cluster, and the blades "
    "share chassis power and cooling, so package + dram is the permanent ceiling",
    "control-group LEAF accounting -- the exporter reports the root only",
    "any cross-platform claim -- every eligible blade carries the same processor",
]


class Report:
    def __init__(self):
        self.items = []
        self.blocked = 0
        self.degraded = 0

    def add(self, name, ok, detail, required=True, claim=None, value=None):
        if ok:
            state = "PASS"
        elif required:
            state = "BLOCKED"
            self.blocked += 1
        else:
            state = "degraded"
            self.degraded += 1
        self.items.append({"check": name, "state": state, "detail": detail,
                           "required": required, "claim_if_absent": claim,
                           "value": value})
        mark = {"PASS": "  ok  ", "BLOCKED": " FAIL ", "degraded": " warn "}[state]
        print(f"[{mark}] {name}")
        print(f"          {detail}")
        if not ok and claim:
            print(f"          claim that dies: {claim}")


def check_prometheus(rep, url):
    try:
        info = promsamples._get(url, "/api/v1/status/buildinfo", {}, timeout=15)
        rep.add("Prometheus reachable", True,
                f"{url}, version {info['version']}", value=info["version"])
        return True
    except Exception as exc:
        rep.add("Prometheus reachable", False,
                f"{url}: {exc}. Is the port-forward up? "
                "kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 19090:9090",
                claim="everything -- no measurement is possible")
        return False


def check_node(rep, url, node):
    try:
        res = promsamples.instant(f'hardware_cpu_seconds_total{{node="{node}"}}', url=url)["result"]
    except Exception as exc:
        rep.add(f"node {node} exports metrics", False, str(exc),
                claim="the whole campaign -- the declared node is not reporting")
        return
    if not res:
        try:
            all_nodes = sorted({s["metric"].get("node") for s in promsamples.instant(
                "hardware_cpu_seconds_total", url=url)["result"]} - {None})
        except Exception:
            all_nodes = []
        rep.add(f"node {node} exports metrics", False,
                f"no series for node={node!r}. Nodes reporting: {', '.join(all_nodes) or 'none'}",
                claim="the whole campaign -- the declared node is not reporting")
    else:
        rep.add(f"node {node} exports metrics", True, f"{len(res)} CPU-mode series")


def check_series(rep, url, node, specs, required):
    for metric, label, min_series, claim in specs:
        sel = f'{metric}{{{label}="{node}"}}'
        try:
            res = promsamples.instant(sel, url=url)["result"]
        except Exception as exc:
            rep.add(metric, False, f"query failed: {exc}", required, claim)
            continue
        if not res:
            # Never report an empty selector as an absent metric without looking.
            try:
                whole = promsamples.instant(metric, url=url)["result"]
            except Exception:
                whole = []
            if whole:
                keys = sorted({k for s in whole for k in s["metric"]} - {"__name__"})
                detail = (f"{len(whole)} series exist but none match {label}={node!r}. "
                          f"Labels present: {', '.join(keys)}")
            else:
                detail = "not exported at all"
            rep.add(metric, False, detail, required, claim)
        elif len(res) < min_series:
            rep.add(metric, False,
                    f"only {len(res)} series, expected at least {min_series}",
                    required, claim)
        else:
            rep.add(metric, True, f"{len(res)} series", required, claim)


def check_proc_modes(rep, url, node):
    try:
        res = promsamples.instant(f'hardware_cpu_seconds_total{{node="{node}"}}', url=url)["result"]
    except Exception as exc:
        rep.add("all eight /proc/stat modes present", False, str(exc),
                claim="`busy` becomes a silent lower bound")
        return
    seen = {s["metric"].get("mode") for s in res}
    missing = [m for m in REQUIRED_PROC_MODES if m not in seen]
    if missing:
        rep.add("all eight /proc/stat modes present", False,
                f"missing: {', '.join(missing)}. A consumer summing what it finds "
                f"reports a lower bound as a measured value",
                claim="`busy`, and therefore every CPU-time result and the whole "
                      "energy-versus-CPU comparison")
    else:
        rep.add("all eight /proc/stat modes present", True,
                f"{', '.join(sorted(seen))}")


def check_cadence_and_presence(rep, url, node, hours):
    rows, slow = [], None
    for role, family, label in measure_cadence.FAMILIES:
        if not family.startswith("hardware_"):
            continue
        r = measure_cadence.summarize(role, family, label, time.time() - hours * 3600,
                                      time.time(), url, node)
        rows.append(r)
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if not ok_rows:
        rep.add("cadence measurable", False, "no reference family returned samples",
                claim="the measurement-interval bound and the aliasing band")
        return None

    thin = []
    for r in ok_rows:
        expected = int((hours * 3600) / r["median_s"])
        # summarize() counts deltas, so observed samples per series is deltas+1
        observed = (r["samples"] / r["series"]) if r["series"] else 0
        frac = observed / expected if expected else 0
        if frac < MIN_PRESENCE:
            thin.append((r["metric"], round(frac, 4)))
    if thin:
        rep.add(f"reference presence over {hours:g} h", False,
                "; ".join(f"{m} at {f}" for m, f in thin),
                claim="any level whose interval overlaps the gap")
    else:
        rep.add(f"reference presence over {hours:g} h", True,
                f"{len(ok_rows)} reference families at or above {MIN_PRESENCE:.0%}")

    uneven = [r for r in ok_rows if r["per_series_median_spread_s"] > 1.0]
    rep.add("reference cadence uniform across series", not uneven,
            ("non-uniform: " + "; ".join(f"{r['metric']} spread {r['per_series_median_spread_s']}s"
                                         for r in uneven))
            if uneven else
            f"all {len(ok_rows)} families uniform",
            required=False,
            claim="expected sample count must then be computed per series, not per family")

    slow = max(ok_rows, key=lambda r: r["median_s"])
    hard, usable = measure_cadence.bounds(slow["median_s"], slow["jitter_s"])
    ok_bands, bad_bands = measure_cadence.alias_bands(slow["median_s"])
    rep.add("measurement-interval bound derivable", True,
            f"slowest reference metric {slow['metric']} at P={slow['median_s']:.2f}s, "
            f"jitter {slow['jitter_s']:.2f}s",
            value={"period_s": slow["median_s"], "jitter_s": slow["jitter_s"],
                   "t_m_minimum_possible_s": round(hard, 1),
                   "t_m_minimum_usable_s": round(usable, 1),
                   "aliasing_bands_s": bad_bands[:5]})
    return {"period_s": slow["median_s"], "jitter_s": slow["jitter_s"],
            "t_m_minimum_possible_s": round(hard, 1),
            "t_m_minimum_usable_s": round(usable, 1),
            "admissible_subinterval_bands_s": ok_bands,
            "aliasing_subinterval_bands_s": bad_bands,
            "slowest_reference_metric": slow["metric"]}


# --- Clock skew thresholds, and where they come from ------------------------
#
# run_sweep.py stamps the declared interval edges with the clock of whatever host
# it runs on, while every sample timestamp comes from the Prometheus server's
# clock. A skew of s shifts every declared edge by s relative to the samples, and
# the edge-offset check asks whether each edge lands within one scrape period of
# its nearest sample. So the skew spends part of that budget before the
# measurement starts.
#
# Expressed as a fraction of the MEASURED scrape period, not in absolute seconds,
# because the budget it eats into is a multiple of that period:
#
#   under 10%   negligible; at a 10 s cadence this is under 1 s
#   10% to 50%  degraded: the edge-offset check still means something, but the
#               skew should be recorded with the session and subtracted
#   over 50%    blocked: an edge could be attributed to the wrong sample, which
#               is the one quantity this design cares most about
SKEW_DEGRADED_FRACTION = 0.10
SKEW_BLOCKED_FRACTION = 0.50

# NTP's own trick: the sample with the smallest round trip carries the least
# asymmetry, so it is the best single estimate of the offset. 9 probes is enough
# for the minimum to be stable and costs under a second.
SKEW_PROBES = 9


def check_clock_skew(rep, url, period_s=None):
    """Measure host-vs-Prometheus clock skew. Not read from NTP, measured."""
    probes = []
    for _ in range(SKEW_PROBES):
        t0 = time.time()
        try:
            res = promsamples.instant("vector(1)", url=url)["result"]
        except Exception as exc:
            rep.add("clock skew host vs Prometheus", False, f"probe failed: {exc}",
                    claim="the interval-edge offset check, which compares declared "
                          "edges against sample timestamps")
            return None
        t1 = time.time()
        # An instant query returns Prometheus's OWN evaluation timestamp, which is
        # exactly what is wanted here -- its clock's notion of now. (It is also why
        # an instant query must never be used to learn when a SAMPLE was taken.)
        probes.append({"skew": float(res[0]["value"][0]) - (t0 + t1) / 2,
                       "rtt": t1 - t0})

    best = min(probes, key=lambda p: p["rtt"])
    skews = sorted(p["skew"] for p in probes)
    median = skews[len(skews) // 2]
    resolution = best["rtt"] / 2  # cannot resolve an offset finer than half the RTT

    value = {"best_estimate_s": round(best["skew"], 4),
             "median_s": round(median, 4),
             "spread_s": round(skews[-1] - skews[0], 4),
             "min_rtt_s": round(best["rtt"], 4),
             "resolution_s": round(resolution, 4),
             "probes": SKEW_PROBES}

    mag = abs(best["skew"])
    detail = (f"best estimate {best['skew'] * 1000:+.1f} ms "
              f"(median {median * 1000:+.1f} ms, spread {(skews[-1] - skews[0]) * 1000:.1f} ms, "
              f"resolution +/-{resolution * 1000:.1f} ms from a {best['rtt'] * 1000:.0f} ms round trip)")

    if mag < resolution:
        detail += " -- below measurement resolution, i.e. no detectable skew"

    if period_s:
        frac = mag / period_s
        detail += f"; {frac:.2%} of the {period_s:.2f} s scrape period"
        value["fraction_of_scrape_period"] = round(frac, 5)
        if frac > SKEW_BLOCKED_FRACTION:
            rep.add("clock skew host vs Prometheus", False,
                    detail + f" -- over {SKEW_BLOCKED_FRACTION:.0%}, an edge could be "
                             "attributed to the wrong sample. Fix NTP on this host",
                    claim="the interval-edge offset check, and therefore the "
                          "validity of every counter difference")
            return value
        if frac > SKEW_DEGRADED_FRACTION:
            rep.add("clock skew host vs Prometheus", False,
                    detail + f" -- over {SKEW_DEGRADED_FRACTION:.0%}; record it with "
                             "the session and subtract it from the declared edges",
                    required=False)
            return value
    rep.add("clock skew host vs Prometheus", True, detail, value=value)
    return value


def check_node_clocks(rep, url):
    """Are the exporter hosts NTP-synced? Informational, via TRUE scrape timestamps.

    Prometheus stamps samples with its own clock, so a node's clock being out does
    not shift any timestamp the analysis uses. It is still worth knowing: a node
    seconds out of sync usually means NTP is broken there, and anything that reads
    a node's own clock -- a generator log, a container timestamp -- would disagree
    with the measurement record.
    """
    try:
        s = promsamples.samples("node_time_seconds", time.time() - 120, time.time(), url=url)
    except Exception as exc:
        rep.add("exporter host clocks synced", False, f"query failed: {exc}", required=False)
        return
    if not s:
        rep.add("exporter host clocks synced", False,
                "node_time_seconds not available (node-exporter absent?)", required=False)
        return
    worst, worst_inst = 0.0, None
    for slot in s.values():
        inst = slot["labels"].get("instance", "?")
        for ts, val in slot["points"]:
            # value is the node's own clock; ts is when Prometheus scraped it, so
            # the difference includes scrape latency and is signed accordingly.
            d = abs(float(val) - ts)
            if d > worst:
                worst, worst_inst = d, inst
    ok = worst < 1.0
    rep.add("exporter host clocks synced", ok,
            f"worst offset {worst * 1000:.0f} ms ({worst_inst}) across {len(s)} hosts"
            + ("" if ok else " -- over 1 s, NTP is likely broken there"),
            required=False)


def check_retention(rep, url, node):
    """Can we still read samples MIN_RETENTION_HOURS old? Measured, not read from config."""
    t = time.time() - MIN_RETENTION_HOURS * 3600
    try:
        s = promsamples.samples(f'hardware_cpu_seconds_total{{node="{node}",mode="idle"}}',
                                t - 300, t, url=url)
    except Exception as exc:
        rep.add(f"samples {MIN_RETENTION_HOURS:g} h old still readable", False, str(exc),
                claim="recovering an archive that was not written promptly")
        return
    have = any(slot["points"] for slot in s.values())
    rep.add(f"samples {MIN_RETENTION_HOURS:g} h old still readable", have,
            "yes" if have else f"nothing at {runmeta.iso(t)}; retention may be shorter "
                               "than it claims, or the exporter restarted",
            claim="recovering an archive that was not written promptly")


def check_wrap(rep, url, node, hours=24):
    """Counter wraps are routine here, so measure the period rather than assume it."""
    end = time.time()
    try:
        s = promsamples.samples(
            f'hardware_rapl_energy_joules_total{{node="{node}",name="package-0"}}',
            end - hours * 3600, end, url=url)
    except Exception as exc:
        rep.add("RAPL wrap characterised", False, str(exc), required=False)
        return None
    for slot in s.values():
        pts = slot["points"]
        if len(pts) < 2:
            continue
        drops, total = [], 0.0
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            a, b = float(v0), float(v1)
            if b < a:
                drops.append(a - b)
            else:
                total += b - a
        span = pts[-1][0] - pts[0][0]
        watts = total / span if span else 0
        if drops and watts > 0:
            rng = max(drops) + watts * statistics.median(
                [t1 - t0 for (t0, _), (t1, _) in zip(pts, pts[1:])])
            period_h = rng / watts / 3600
            rep.add("RAPL wrap characterised", True,
                    f"{len(drops)} wraps in {hours} h; counter range ~{rng:.0f} J; "
                    f"at the observed {watts:.2f} W that is one wrap every {period_h:.1f} h. "
                    f"Shorter under load -- the analysis MUST unwrap",
                    required=False,
                    value={"wraps_observed": len(drops), "range_joules": round(rng, 1),
                           "mean_watts": round(watts, 2),
                           "wrap_period_hours_at_this_power": round(period_h, 2)})
            return {"range_joules": round(rng, 1),
                    "wrap_period_hours_at_idle": round(period_h, 2)}
        if watts > 0:
            rep.add("RAPL wrap characterised", True,
                    f"no wrap in {hours} h at {watts:.2f} W; range not observable from "
                    f"this window, so take it from max_energy_range_uj before analysing",
                    required=False)
            return None
    rep.add("RAPL wrap characterised", False, "no usable package-0 samples", required=False)
    return None


def check_grafana(rep, url, token):
    if not url or not token:
        rep.add("Grafana supervision wired", False,
                "GRAFANA_URL / GRAFANA_TOKEN not set. A sweep still runs; it just "
                "cannot be watched live",
                required=False, claim="live supervision only, never a result")
        return
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/api/org",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            org = json.load(r)
    except (urllib.error.URLError, OSError) as exc:
        rep.add("Grafana supervision wired", False, f"{url}: {exc}", required=False)
        return
    detail = f"{url}, org {org.get('name')!r}"
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/dashboards/uid/load-sweep-supervision",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10):
            detail += "; supervision dashboard present"
    except Exception:
        detail += "; dashboard MISSING -- run make_dashboard.py"
    rep.add("Grafana supervision wired", True, detail, required=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=promsamples.DEFAULT_URL)
    ap.add_argument("--node", required=True)
    ap.add_argument("--hours", type=float, default=1.0,
                    help="window for the cadence and presence checks")
    ap.add_argument("--grafana-url", default=os.environ.get("GRAFANA_URL"))
    ap.add_argument("--grafana-token", default=os.environ.get("GRAFANA_TOKEN"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--skip-wrap", action="store_true",
                    help="skip the 24 h wrap scan (the slowest check)")
    args = ap.parse_args()

    started = time.time()
    print("=" * 78)
    print(f"INFRASTRUCTURE STATUS  node={args.node}  {runmeta.iso(started)}")
    print("=" * 78)
    print("\n-- prerequisites the infrastructure owns " + "-" * 36)
    rep = Report()

    if not check_prometheus(rep, args.url):
        print("\nBLOCKED: nothing else can be checked without Prometheus.")
        return 2

    check_node(rep, args.url, args.node)
    check_series(rep, args.url, args.node, REQUIRED_SERIES, required=True)
    check_proc_modes(rep, args.url, args.node)
    check_series(rep, args.url, args.node, OPTIONAL_SERIES, required=False)
    derived = check_cadence_and_presence(rep, args.url, args.node, args.hours)
    # After the cadence check, so the skew can be judged against the measured
    # scrape period rather than against an absolute number of seconds.
    skew = check_clock_skew(rep, args.url, derived["period_s"] if derived else None)
    check_node_clocks(rep, args.url)
    check_retention(rep, args.url, args.node)
    wrap = None if args.skip_wrap else check_wrap(rep, args.url, args.node)
    check_grafana(rep, args.grafana_url, args.grafana_token)

    if derived:
        print("\n-- measured values the researcher must USE " + "-" * 34)
        print(f"  scrape period                 {derived['period_s']:.2f} s "
              f"(jitter {derived['jitter_s']:.2f} s)")
        print(f"  t_m minimum possible          {derived['t_m_minimum_possible_s']:.0f} s "
              f"-- below this a split-half test cannot be computed")
        print(f"  t_m minimum usable            {derived['t_m_minimum_usable_s']:.0f} s "
              f"-- CONVENTION, not a derivation: 8 samples per half")
        print(f"  split lengths that alias      " +
              ", ".join(f"{lo:.0f}-{hi:.0f}s"
                        for lo, hi in derived["aliasing_subinterval_bands_s"][:5]) + " ...")
    if skew:
        print(f"  clock skew vs Prometheus      {skew['best_estimate_s'] * 1000:+.1f} ms "
              f"(resolution +/-{skew['resolution_s'] * 1000:.0f} ms) -- "
              f"re-measure if the sweep runs from another host")
    if wrap:
        print(f"  RAPL counter range            {wrap['range_joules']:.0f} J "
              f"-- wraps every ~{wrap['wrap_period_hours_at_idle']:.1f} h at idle")

    print("\n-- the researcher must DERIVE these, never inherit them " + "-" * 21)
    for name, how in RESEARCHER_MUST_DERIVE:
        print(f"  [ ] {name}")
        print(f"        {how}")

    print("\n-- this infrastructure deliberately does NOT provide " + "-" * 24)
    for item in NOT_PROVIDED:
        print(f"  x  {item}")

    if rep.blocked:
        verdict, code = "BLOCKED", 2
        tail = (f"{rep.blocked} required check(s) failed. A campaign run now would "
                "produce numbers that have to be discarded.")
    elif rep.degraded:
        verdict, code = "DEGRADED", 1
        tail = (f"all required checks passed; {rep.degraded} optional item(s) "
                "unavailable. A campaign is valid, with the claims named above lost.")
    else:
        verdict, code = "READY", 0
        tail = "every check passed. The measurement half is the researcher's to build."

    print("\n" + "=" * 78)
    print(f"VERDICT: {verdict}")
    print(f"  {tail}")
    print("=" * 78)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({
                "checked_at": started, "checked_at_iso": runmeta.iso(started),
                "node": args.node, "prometheus": args.url,
                "verdict": verdict, "exit_code": code,
                "blocked": rep.blocked, "degraded": rep.degraded,
                "checks": rep.items,
                "measured_values_to_use": derived,
                "clock_skew": skew,
                "rapl_wrap": wrap,
                "researcher_must_derive": [{"value": n, "how": h}
                                           for n, h in RESEARCHER_MUST_DERIVE],
                "not_provided": NOT_PROVIDED,
            }, fh, indent=2)
        print(f"wrote {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
