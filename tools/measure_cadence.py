#!/usr/bin/env python3
"""Measure the real scrape cadence per metric, and derive the two bounds that follow.

Nothing here is assumed from a ServiceMonitor or a scrape_interval setting. The
cadence is read off the true sample timestamps, per series, because on this class
of cluster it is NOT uniform: one exporter family can sit at exactly 10 s while
kubelet/cAdvisor runs at ~20 s and irregular per series. Assuming one cadence for
all of them once reported a fully-present exporter as 70% present.

Two values are derived from the measured cadence, and both ship with their
derivation rather than as constants:

  1. the lower bound on the measurement interval t_m
  2. the band of split sub-interval lengths that ALIAS against the grid

Usage:
    python3 -I measure_cadence.py --hours 2 --node node2
    python3 -I measure_cadence.py --hours 2 --json cadence.json
"""

import argparse
import json
import os
import statistics
import sys
import time

# Run these tools with `python3 -I` so nothing is imported from the data
# directory you happen to be standing in. -I also drops the script's own
# directory from sys.path, so the sibling module is added back explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promsamples  # noqa: E402
import runmeta  # noqa: E402

# Metric families the method consumes, by role, each with the label that carries
# the node identity FOR THAT EXPORTER. They do not agree, and that disagreement is
# not cosmetic: filtering every family on `node` returns zero series for the ones
# that label it otherwise, and zero series reads as "metric absent" unless the tool
# is built to tell the two apart. Verified on this cluster 2026-10-07 --
# hardware-exporter and Scaphandre use `node`, Kepler uses `node_name`, and
# node-exporter and cAdvisor carry no node label at all, only `instance`.
#
# Kepler and Scaphandre are listed so their cadence is known well enough to
# EXCLUDE them coherently, never because they are read as a reference.
FAMILIES = [
    ("energy reference",       "hardware_rapl_energy_joules_total",               "node"),
    ("cgroup root closure",    "hardware_cgroup_root_cpu_seconds_total",          "node"),
    ("node CPU modes",         "hardware_cpu_seconds_total",                      "node"),
    ("per-core CPU modes",     "hardware_cpu_core_seconds_total",                 "node"),
    ("C-state residency",      "hardware_cpu_cstate_residency_microseconds_total", "node"),
    ("CPU frequency",          "hardware_cpu_frequency_average_hertz",            "node"),
    ("thermal throttle",       "hardware_cpu_thermal_throttle_total",             "node"),
    ("softirq/NAPI witness",   "hardware_softnet_squeezed_total",                 "node"),
    ("kthread CPU",            "hardware_kthread_cpu_seconds_total",              "node"),
    ("node-exporter CPU",      "node_cpu_seconds_total",                          None),
    ("cAdvisor container CPU", "container_cpu_usage_seconds_total",               "node"),
    ("under test: Kepler",     "kepler_node_cpu_joules_total",                    "node_name"),
    ("under test: Scaphandre", "scaph_host_energy_microjoules",                   "node"),
]

# --- Derivation of the t_m lower bound -------------------------------------
#
# A counter difference needs two samples: one at each interval edge. The split-half
# stationarity test needs a valid difference in EACH half, so each half needs two
# samples of its own: four samples, spanning 3 cadence periods.
#
# Each declared edge may fall up to one full period before the first usable sample
# (and after the last), because nothing guarantees a sample lands on a declared
# instant: +2 periods.
#
# Hard floor      = (3 + 2) * P + observed jitter  = 5P + jitter
# At P = 10 s this gives ~50 s, consistent with the ~58 s floor derived
# independently on the reference cluster.
#
# The hard floor is the point below which the test is impossible, NOT the point
# where it is useful: a split-half comparison of two points against two points has
# no power. SAMPLES_PER_HALF sets the usable floor and is the knob to argue about.
# 8 per half -> (2*8 - 1 + 2) * P = 17P = 170 s at P = 10 s, which is why a t_m of
# a few hundred seconds is the right order and 60 s is not.
MIN_SAMPLES_PER_HALF_HARD = 2
SAMPLES_PER_HALF_USABLE = 8

# --- Derivation of the aliasing band ---------------------------------------
#
# A split sub-interval of length L on a grid of period P contains floor(L/P) or
# ceil(L/P) samples depending on phase. If L/P is an integer the count is stable;
# if it is halfway between integers the count alternates by one every sub-interval,
# which is the maximum fabricated high/low swing. The error in a per-sub-interval
# mean is ~1/(L/P), so the swing matters most at small L/P.
#
# A length is ADMISSIBLE if L/P is within ALIAS_TOLERANCE of an integer and that
# integer is at least MIN_PERIODS_PER_SUBINTERVAL. The tolerance is set at 0.10
# because at L/P = k + 0.10 at most one sub-interval in ten gains or loses a sample,
# which is below the jitter already present in the cadence itself.
ALIAS_TOLERANCE = 0.10
MIN_PERIODS_PER_SUBINTERVAL = 2


def cadence_of(points):
    """Inter-sample intervals for one series, in seconds."""
    return [b - a for (a, _), (b, _) in zip(points, points[1:])]


def summarize(name, family, node_label, start, end, url, node=None):
    sel = f'{family}{{{node_label}="{node}"}}' if (node and node_label) else family
    try:
        series = promsamples.samples(sel, start, end, url=url)
    except Exception as exc:  # a family may not exist on this cluster at all
        return {"role": name, "metric": family, "status": f"query failed: {exc}"}
    if not series:
        # An empty result has two completely different causes and they must never
        # be collapsed: the metric is genuinely not exported, or it is exported and
        # this selector does not match it. Reporting the second as the first is the
        # "an absent series is not a zero" failure, and the fix is to go and look.
        try:
            whole = promsamples.instant(family, url=url)["result"]
        except Exception:
            whole = []
        if not whole:
            return {"role": name, "metric": family, "status": "ABSENT: not exported"}
        keys = sorted(set(k for s in whole for k in s["metric"]) - {"__name__"})
        return {"role": name, "metric": family,
                "status": f"SELECTOR MISMATCH: {len(whole)} series exist, "
                          f"none match {node_label}={node!r}; labels present: {','.join(keys)}",
                "series_unfiltered": len(whole), "labels": keys}

    deltas, per_series_median, short = [], [], 0
    for slot in series.values():
        d = cadence_of(slot["points"])
        if not d:
            short += 1
            continue
        deltas.extend(d)
        per_series_median.append(statistics.median(d))
    if not deltas:
        return {"role": name, "metric": family, "status": "present but <2 samples/series",
                "series": len(series)}

    deltas.sort()
    med = statistics.median(deltas)
    # Jitter as the spread of the middle 90% rather than min/max, so one restart
    # or one missed scrape does not define the margin a bound is built on.
    p05, p95 = deltas[int(0.05 * len(deltas))], deltas[int(0.95 * len(deltas))]
    return {
        "role": name,
        "metric": family,
        "status": "ok",
        "series": len(series),
        "series_too_short": short,
        "samples": len(deltas) + len(per_series_median),
        "median_s": round(med, 3),
        "p05_s": round(p05, 3),
        "p95_s": round(p95, 3),
        "min_s": round(deltas[0], 3),
        "max_s": round(deltas[-1], 3),
        # Uniform across series? If not, no single cadence may be assumed for it.
        "per_series_median_spread_s": round(max(per_series_median) - min(per_series_median), 3),
        "jitter_s": round(p95 - p05, 3),
    }


def bounds(period, jitter):
    hard = (2 * MIN_SAMPLES_PER_HALF_HARD - 1 + 2) * period + jitter
    usable = (2 * SAMPLES_PER_HALF_USABLE - 1 + 2) * period + jitter
    return hard, usable


def alias_bands(period, up_to_periods=12):
    """Admissible and forbidden split sub-interval lengths, in seconds."""
    ok, bad = [], []
    for k in range(MIN_PERIODS_PER_SUBINTERVAL, up_to_periods + 1):
        lo = (k - ALIAS_TOLERANCE) * period
        hi = (k + ALIAS_TOLERANCE) * period
        ok.append((round(lo, 1), round(hi, 1), k))
        if k < up_to_periods:
            bad.append((round(hi, 1), round(((k + 1) - ALIAS_TOLERANCE) * period, 1)))
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=promsamples.DEFAULT_URL)
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--node", default=None, help="restrict to one node label")
    ap.add_argument("--json", default=None, help="write the full result here")
    args = ap.parse_args()

    end = time.time()
    start = end - args.hours * 3600
    print(f"window: {args.hours} h ending {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end))}"
          + (f"   node={args.node}" if args.node else "   all nodes"))
    print()
    hdr = f"{'metric':<48}{'series':>7}{'median':>9}{'p05':>8}{'p95':>8}{'jitter':>8}{'spread':>8}"
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for role, family, node_label in FAMILIES:
        r = summarize(role, family, node_label, start, end, args.url, args.node)
        rows.append(r)
        if r["status"] != "ok":
            print(f"{family:<48}{'-':>7}   {r['status']}")
            continue
        print(f"{family:<48}{r['series']:>7}{r['median_s']:>9.2f}{r['p05_s']:>8.2f}"
              f"{r['p95_s']:>8.2f}{r['jitter_s']:>8.2f}{r['per_series_median_spread_s']:>8.2f}")

    ok = [r for r in rows if r["status"] == "ok"]
    ref = [r for r in ok if r["metric"].startswith("hardware_")]
    if not ref:
        print("\nno reference-plane series found; cannot derive bounds", file=sys.stderr)
        return 1

    # The bound must hold for the SLOWEST reference metric the analysis consumes.
    # Taking the fastest, or an average, produces an interval that is adequate for
    # some responses and silently too short for others.
    slow = max(ref, key=lambda r: r["median_s"])
    period, jitter = slow["median_s"], slow["jitter_s"]
    hard, usable = bounds(period, jitter)
    ok_bands, bad_bands = alias_bands(period)

    print(f"\nslowest reference metric: {slow['metric']}  P = {period:.2f} s, jitter = {jitter:.2f} s")
    print(f"  t_m hard floor   >= {hard:7.1f} s   ({MIN_SAMPLES_PER_HALF_HARD} samples per half; test possible, no power)")
    print(f"  t_m usable floor >= {usable:7.1f} s   ({SAMPLES_PER_HALF_USABLE} samples per half)")
    print(f"  admissible split sub-intervals: " +
          ", ".join(f"{lo:.0f}-{hi:.0f}s(k={k})" for lo, hi, k in ok_bands[:6]))
    print(f"  ALIASING band, do not split here: " +
          ", ".join(f"{lo:.0f}-{hi:.0f}s" for lo, hi in bad_bands[:5]))

    uneven = [r for r in ok if r["per_series_median_spread_s"] > 1.0]
    if uneven:
        print("\n  cadence is NOT uniform across series for:")
        for r in uneven:
            print(f"    {r['metric']}  per-series median spread {r['per_series_median_spread_s']:.1f} s"
                  f"  (min {r['min_s']:.1f}, max {r['max_s']:.1f})")
        print("  -> expected sample count must be computed per series for these, not per family.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({
                "measured_at": end,
                "measured_at_iso": runmeta.iso(end),
                "timestamps_note": runmeta.ISO_NOTE,
                "window_hours": args.hours,
                "node": args.node,
                "metrics": rows,
                "derivation": {
                    "slowest_reference_metric": slow["metric"],
                    "period_s": period,
                    "jitter_s": jitter,
                    "t_m_hard_floor_s": round(hard, 1),
                    "t_m_usable_floor_s": round(usable, 1),
                    "samples_per_half_hard": MIN_SAMPLES_PER_HALF_HARD,
                    "samples_per_half_usable": SAMPLES_PER_HALF_USABLE,
                    "alias_tolerance": ALIAS_TOLERANCE,
                    "admissible_subinterval_bands_s": ok_bands,
                    "aliasing_subinterval_bands_s": bad_bands,
                },
            }, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
