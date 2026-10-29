"""Per-level processing: counter differences, power, busy cores, validity checks.

Usage:
    python3 -I scripts/analyse_levels.py data/nginx-rerun2
    python3 -I scripts/analyse_levels.py data/stressng-sweep-05

Reads levels.jsonl and the samples CSV from the given directory.
Writes level-results.csv to the same directory.
"""

import csv
import json
import math
import pathlib
import sys

CADENCE_S = 10.0
RAPL_WRAP_J = 262144.0  # 2^18 joules, measured on this cluster

BUSY_MODES = ("user", "nice", "system", "irq", "softirq", "steal")

def load_levels(levels_path):
    levels = []
    with open(levels_path) as f:
        for line in f:
            line = line.strip()
            if line:
                levels.append(json.loads(line))
    return levels


def load_samples(directory):
    # Load the samples CSV. Handles both naming conventions.
    csvs = list(directory.glob("samples*.csv"))
    if not csvs:
        sys.exit(f"No samples CSV found in {directory}")
    path = csvs[0]
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["timestamp"] = float(row["timestamp"])
            row["value"] = float(row["value"])
            row["_labels"] = json.loads(row["labels"])
            rows.append(row)
    return rows


def load_samples_grouped(directory):
    # Load samples and group by metric name, sorted by timestamp.
    rows = load_samples(directory)
    by_metric = {}
    for r in rows:
        by_metric.setdefault(r["metric"], []).append(r)
    for v in by_metric.values():
        v.sort(key=lambda r: r["timestamp"])
    return by_metric


def series_key(row):
    # Unique series identity: metric + distinguishing label.
    m = row["metric"]
    labels = row["_labels"]
    if m == "hardware_rapl_energy_joules_total":
        return (m, labels.get("name", ""))
    elif m in ("hardware_cpu_seconds_total", "hardware_cgroup_root_cpu_seconds_total"):
        return (m, labels.get("mode", ""))
    elif m == "hardware_cpu_cstate_residency_microseconds_total":
        return (m, labels.get("state", ""))
    else:
        return (m, "")


def select_samples(all_samples, t_start, t_end, metric, label_value=None):
    # Select samples for one metric+label within a time window.
    out = []
    for r in all_samples:
        if r["metric"] != metric:
            continue
        if r["timestamp"] < t_start or r["timestamp"] > t_end:
            continue
        if label_value is not None:
            labels = r["_labels"]
            if metric == "hardware_rapl_energy_joules_total":
                if labels.get("name") != label_value:
                    continue
            elif metric in ("hardware_cpu_seconds_total",
                            "hardware_cgroup_root_cpu_seconds_total"):
                if labels.get("mode") != label_value:
                    continue
        out.append(r)
    out.sort(key=lambda r: r["timestamp"])
    return out


def snap_edges(samples, t_m_start, t_m_end):
    # Find the first sample >= t_m_start and last sample <= t_m_end.
    first = None
    last = None
    for s in samples:
        if s["timestamp"] >= t_m_start and first is None:
            first = s
        if s["timestamp"] <= t_m_end:
            last = s
    return first, last


def counter_diff(v_end, v_start):
    # Difference with RAPL wrap handling.
    d = v_end - v_start
    if d < 0:
        d += RAPL_WRAP_J
    return d


def counter_diff_cpu(v_end, v_start):
    # CPU counters don't wrap in practice over 300 s.
    d = v_end - v_start
    return d


def unwrap_energy(samples):
    # Return list of unwrapped energy values, with wrap count.
    if not samples:
        return [], 0
    vals = [samples[0]["value"]]
    wraps = 0
    for i in range(1, len(samples)):
        d = samples[i]["value"] - samples[i-1]["value"]
        if d < 0:
            wraps += 1
            d += RAPL_WRAP_J
        vals.append(vals[-1] + d)
    return vals, wraps


def check_no_reset(samples_in_interval, is_energy=False):
    # Check that no counter reset occurs. RAPL wraps are expected, not resets.
    # A wrap: value drops by ~RAPL_WRAP_J. A reset: value drops to near zero.
    for i in range(1, len(samples_in_interval)):
        d = samples_in_interval[i]["value"] - samples_in_interval[i-1]["value"]
        if is_energy and d < 0:
            # A wrap produces d ≈ -(RAPL_WRAP_J - small_energy), so d > -RAPL_WRAP_J.
            # A true reset drops to near zero: d ≈ -current_value.
            # Accept if it looks like a single wrap.
            recovered = d + RAPL_WRAP_J
            if recovered < 0 or recovered > RAPL_WRAP_J * 0.1:
                return False
        elif not is_energy and d < 0:
            return False
    return True


def check_series_constant(all_samples, t_start, t_end):
    # Check that the set of present series is identical at start, mid, end.
    t_mid = (t_start + t_end) / 2
    margin = CADENCE_S * 1.5

    def series_at(t):
        keys = set()
        for r in all_samples:
            if abs(r["timestamp"] - t) <= margin:
                keys.add(series_key(r))
        return keys

    s_start = series_at(t_start)
    s_mid = series_at(t_mid)
    s_end = series_at(t_end)

    if not s_start or not s_mid or not s_end:
        return False
    return s_start == s_mid == s_end


def check_edge_offsets(snap_start_t, snap_end_t, t_m_start, t_m_end):
    # Both edge offsets must be within one cadence period.
    off_start = abs(snap_start_t - t_m_start)
    off_end = abs(snap_end_t - t_m_end)
    return off_start <= CADENCE_S and off_end <= CADENCE_S, off_start, off_end


def check_split_half(samples_in_interval, is_energy=False):
    # Split-half stationarity: compare mean rate of first half vs second half.
    # Uses unwrapped values for energy counters.
    if len(samples_in_interval) < 4:
        return False, 0.0
    n = len(samples_in_interval)
    mid = n // 2

    if is_energy:
        unwrapped, _ = unwrap_energy(samples_in_interval)
    else:
        unwrapped = [s["value"] for s in samples_in_interval]
    times = [s["timestamp"] for s in samples_in_interval]

    def half_rate(vals, ts):
        if len(vals) < 2:
            return None
        dt = ts[-1] - ts[0]
        if dt <= 0:
            return None
        return (vals[-1] - vals[0]) / dt

    r1 = half_rate(unwrapped[:mid+1], times[:mid+1])
    r2 = half_rate(unwrapped[mid:], times[mid:])

    if r1 is None or r2 is None:
        return False, 0.0

    overall = (r1 + r2) / 2
    if overall == 0:
        return True, 0.0
    ratio = abs(r1 - r2) / abs(overall)
    # 10% threshold for stationarity
    return ratio < 0.10, r1 - r2


def select_rapl_samples(all_samples, t_m_start, t_m_end, name="package-0"):
    # Select RAPL energy samples for one domain within the measurement window.
    return sorted(
        [r for r in all_samples
         if r["metric"] == "hardware_rapl_energy_joules_total"
         and r["_labels"].get("name") == name
         and t_m_start - CADENCE_S <= r["timestamp"] <= t_m_end + CADENCE_S],
        key=lambda r: r["timestamp"],
    )


def compute_power(all_samples, t_m_start, t_m_end):
    # Compute package power (W) from RAPL samples over a measurement window.
    # Returns (power_w, energy_j, elapsed_s, snap_start_t, snap_end_t, wraps).
    pkg = select_rapl_samples(all_samples, t_m_start, t_m_end)
    first, last = snap_edges(pkg, t_m_start, t_m_end)
    if not first or not last or first is last:
        return float("nan"), float("nan"), float("nan"), t_m_start, t_m_start, 0
    interval = [s for s in pkg
                if first["timestamp"] <= s["timestamp"] <= last["timestamp"]]
    unwrapped, wraps = unwrap_energy(interval) if interval else ([], 0)
    if len(unwrapped) < 2:
        return float("nan"), float("nan"), float("nan"), t_m_start, t_m_start, 0
    energy_j = unwrapped[-1] - unwrapped[0]
    elapsed_s = last["timestamp"] - first["timestamp"]
    return energy_j / elapsed_s, energy_j, elapsed_s, first["timestamp"], last["timestamp"], wraps


def compute_busy_cores(all_samples, t_m_start, t_m_end):
    # Compute busy cores from /proc/stat modes over a measurement window.
    # Returns (busy_cores, all_modes_present).
    busy_total = 0.0
    elapsed = 0.0
    all_present = True
    for mode in BUSY_MODES:
        ms = sorted(
            [r for r in all_samples
             if r["metric"] == "hardware_cpu_seconds_total"
             and r["_labels"].get("mode") == mode
             and t_m_start - CADENCE_S <= r["timestamp"] <= t_m_end + CADENCE_S],
            key=lambda r: r["timestamp"],
        )
        first, last = snap_edges(ms, t_m_start, t_m_end)
        if first and last and first is not last:
            busy_total += counter_diff_cpu(last["value"], first["value"])
            elapsed = last["timestamp"] - first["timestamp"]
        else:
            all_present = False
    if elapsed > 0:
        return busy_total / elapsed, all_present
    return float("nan"), False


def run_level_checks(all_samples, t_m_start, t_m_end):
    # Run the four validity checks on one level's samples.
    # Returns dict with check results.
    pkg = select_rapl_samples(all_samples, t_m_start, t_m_end)
    first, last = snap_edges(pkg, t_m_start, t_m_end)
    if not first or not last:
        return {"no_reset": False, "series_constant": False,
                "edge_offset": False, "split_half": False, "split_half_diff": 0.0}
    c_reset = check_no_reset(pkg, is_energy=True)
    c_series = check_series_constant(all_samples, first["timestamp"], last["timestamp"])
    c_edge, off_s, off_e = check_edge_offsets(
        first["timestamp"], last["timestamp"], t_m_start, t_m_end)
    c_split, split_diff = check_split_half(pkg, is_energy=True)
    return {
        "no_reset": c_reset,
        "series_constant": c_series,
        "edge_offset": c_edge,
        "split_half": c_split,
        "split_half_diff": split_diff,
        "edge_offset_start": off_s,
        "edge_offset_end": off_e,
    }


def process_campaign(directory):
    directory = pathlib.Path(directory)
    levels = load_levels(directory / "levels.jsonl")
    print(f"Loading samples from {directory}...")
    all_samples = load_samples(directory)
    print(f"  {len(all_samples)} rows loaded, {len(levels)} levels")

    # Pre-index samples by metric for faster lookup
    by_metric = {}
    for r in all_samples:
        by_metric.setdefault(r["metric"], []).append(r)
    for v in by_metric.values():
        v.sort(key=lambda r: r["timestamp"])

    results = []

    for i, level in enumerate(levels):
        pos = level["position"]
        lam = level["lambda"]
        kind = level["kind"]
        t_m_start = level["t_m_start"]
        t_m_end = level["t_m_end"]

        # Preceding lambda for carry-over regression
        if i > 0:
            preceding_lambda = levels[i-1]["lambda"]
        else:
            preceding_lambda = None

        # Wide window for sample selection (one cadence margin each side)
        t_wide_start = t_m_start - CADENCE_S
        t_wide_end = t_m_end + CADENCE_S

        # RAPL package energy
        pkg_samples = [r for r in by_metric.get("hardware_rapl_energy_joules_total", [])
                       if r["_labels"].get("name") == "package-0"
                       and t_wide_start <= r["timestamp"] <= t_wide_end]
        pkg_samples.sort(key=lambda r: r["timestamp"])

        pkg_first, pkg_last = snap_edges(pkg_samples, t_m_start, t_m_end)

        # Unwrap the full interval for correct energy computation
        pkg_in_interval = [s for s in pkg_samples
                           if s["timestamp"] >= (pkg_first["timestamp"] if pkg_first else t_m_start)
                           and s["timestamp"] <= (pkg_last["timestamp"] if pkg_last else t_m_end)]
        pkg_unwrapped, pkg_wraps = unwrap_energy(pkg_in_interval) if pkg_in_interval else ([], 0)

        if pkg_first and pkg_last and pkg_first is not pkg_last and len(pkg_unwrapped) >= 2:
            energy_j = pkg_unwrapped[-1] - pkg_unwrapped[0]
            elapsed_s = pkg_last["timestamp"] - pkg_first["timestamp"]
            power_w = energy_j / elapsed_s
            snap_start_t = pkg_first["timestamp"]
            snap_end_t = pkg_last["timestamp"]
        else:
            energy_j = elapsed_s = power_w = float("nan")
            snap_start_t = snap_end_t = t_m_start
            pkg_wraps = 0

        # RAPL dram energy
        dram_samples = [r for r in by_metric.get("hardware_rapl_energy_joules_total", [])
                        if r["_labels"].get("name") == "dram"
                        and t_wide_start <= r["timestamp"] <= t_wide_end]
        dram_samples.sort(key=lambda r: r["timestamp"])
        dram_first, dram_last = snap_edges(dram_samples, t_m_start, t_m_end)
        if dram_first and dram_last and dram_first is not dram_last:
            dram_energy_j = counter_diff(dram_last["value"], dram_first["value"])
            dram_elapsed = dram_last["timestamp"] - dram_first["timestamp"]
            dram_power_w = dram_energy_j / dram_elapsed
        else:
            dram_energy_j = dram_power_w = float("nan")

        # CPU busy cores
        busy_total = 0.0
        cpu_elapsed = 0.0
        all_modes_present = True
        for mode in BUSY_MODES:
            mode_samples = [r for r in by_metric.get("hardware_cpu_seconds_total", [])
                            if r["_labels"].get("mode") == mode
                            and t_wide_start <= r["timestamp"] <= t_wide_end]
            mode_samples.sort(key=lambda r: r["timestamp"])
            m_first, m_last = snap_edges(mode_samples, t_m_start, t_m_end)
            if m_first and m_last and m_first is not m_last:
                busy_total += counter_diff_cpu(m_last["value"], m_first["value"])
                cpu_elapsed = m_last["timestamp"] - m_first["timestamp"]
            else:
                all_modes_present = False

        if cpu_elapsed > 0:
            busy_cores = busy_total / cpu_elapsed
        else:
            busy_cores = float("nan")

        # cgroup root usage
        cg_samples = [r for r in by_metric.get("hardware_cgroup_root_cpu_seconds_total", [])
                      if r["_labels"].get("mode") == "usage"
                      and t_wide_start <= r["timestamp"] <= t_wide_end]
        cg_samples.sort(key=lambda r: r["timestamp"])
        cg_first, cg_last = snap_edges(cg_samples, t_m_start, t_m_end)
        if cg_first and cg_last and cg_first is not cg_last:
            cg_usage = counter_diff_cpu(cg_last["value"], cg_first["value"])
            cg_elapsed = cg_last["timestamp"] - cg_first["timestamp"]
            cgroup_root_cores = cg_usage / cg_elapsed
        else:
            cgroup_root_cores = float("nan")

        # Validity checks
        c_no_reset = check_no_reset(pkg_samples, is_energy=True)

        c_series = check_series_constant(all_samples, snap_start_t, snap_end_t)

        c_edge, off_start, off_end = check_edge_offsets(
            snap_start_t, snap_end_t, t_m_start, t_m_end)

        c_split, split_diff = check_split_half(pkg_samples, is_energy=True)

        results.append({
            "position": pos,
            "lambda": lam,
            "kind": kind,
            "power_w": round(power_w, 4),
            "dram_power_w": round(dram_power_w, 4),
            "busy_cores": round(busy_cores, 6),
            "cgroup_root_cores": round(cgroup_root_cores, 6),
            "energy_j": round(energy_j, 4),
            "dram_energy_j": round(dram_energy_j, 4),
            "elapsed_s": round(elapsed_s, 3),
            "edge_offset_start_s": round(off_start, 3),
            "edge_offset_end_s": round(off_end, 3),
            "check_no_reset": "pass" if c_no_reset else "FAIL",
            "check_series_constant": "pass" if c_series else "FAIL",
            "check_edge_offset": "pass" if c_edge else "FAIL",
            "check_split_half": "pass" if c_split else "FAIL",
            "split_half_diff": round(split_diff, 6),
            "rapl_wraps": pkg_wraps,
            "all_busy_modes_present": "yes" if all_modes_present else "NO",
            "preceding_lambda": preceding_lambda if preceding_lambda is not None else "",
        })

        status = "OK" if all(r.startswith("p") for r in
                    [results[-1]["check_no_reset"], results[-1]["check_series_constant"],
                     results[-1]["check_edge_offset"], results[-1]["check_split_half"]]) else "FAIL"
        print(f"  level {pos:2d}  λ={lam:8.1f}  {kind:12s}  "
              f"P={power_w:7.2f} W  busy={busy_cores:.4f} c  [{status}]")

    # Write output
    out_path = directory / "level-results.csv"
    fieldnames = list(results[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)
    print(f"\nWrote {out_path} ({len(results)} levels)")

    # Session-level checks
    print("\n── Session-level checks ──")

    # Split-half sign distribution
    signs = [r["split_half_diff"] for r in results if not math.isnan(r["split_half_diff"])]
    n_pos = sum(1 for s in signs if s > 0)
    n_neg = sum(1 for s in signs if s < 0)
    print(f"Split-half sign: {n_pos} positive, {n_neg} negative (systematic if lopsided)")

    # Carry-over regression for idle levels
    idle_levels = [(r["preceding_lambda"], r["power_w"])
                   for r in results
                   if r["kind"] == "active-idle" and r["preceding_lambda"] != ""]
    if len(idle_levels) >= 2:
        xs = [float(x) for x, _ in idle_levels]
        ys = [y for _, y in idle_levels]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den > 0:
            slope = num / den
            print(f"Carry-over regression: slope = {slope:.6f} W per unit λ "
                  f"({'near zero' if abs(slope) < 0.001 else 'NONZERO'})")
            for x, y in zip(xs, ys):
                print(f"  preceding λ={x:.0f}  idle P={y:.2f} W")
        else:
            print("Carry-over regression: insufficient variation in preceding λ")
    else:
        print("Carry-over regression: not enough idle levels with known predecessors")

    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -I scripts/analyse_levels.py <campaign-dir>")
        sys.exit(1)
    process_campaign(sys.argv[1])
