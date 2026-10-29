"""Section 6.6: Fault injections — validate the protocol's checks.

Usage:
    python3 -I scripts/analyse_fault_injections.py

Three faults are injected into real campaign data, one level at a time:
  1. Absent series: remove the 'softirq' CPU mode from one level.
  2. Counter reset: zero the RAPL energy counter mid-interval.
  3. Non-stationary level: splice samples from two different load levels.

Each fault is reported with two outcomes:
  - Whether the protocol detected it before the level was fitted.
  - What the result would have been had the fault gone undetected.
"""

import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from analyse_levels import (
    load_levels, load_samples, compute_power, compute_busy_cores,
    run_level_checks, CADENCE_S,
)

ROOT = pathlib.Path(__file__).parent.parent

CAMPAIGNS = [
    ("nginx-rerun2", ROOT / "data" / "nginx-rerun2"),
    ("stressng-sweep-05", ROOT / "data" / "stressng-sweep-05"),
]


def pick_target_level(levels, target_lambda):
    # Pick the first load level at the given lambda.
    for lev in levels:
        if lev["lambda"] == target_lambda and lev["kind"] == "load":
            return lev
    return None


def fault_absent_series(all_samples, level, mode="softirq"):
    # Remove one CPU mode from one level's measurement window.
    t_start = level["t_m_start"] - CADENCE_S
    t_end = level["t_m_end"] + CADENCE_S
    corrupted = []
    removed = 0
    for s in all_samples:
        if (s["metric"] == "hardware_cpu_seconds_total"
                and s["_labels"].get("mode") == mode
                and t_start <= s["timestamp"] <= t_end):
            removed += 1
            continue
        corrupted.append(s)
    return corrupted, removed


def fault_counter_reset(all_samples, level):
    # Zero the RAPL package energy counter at the midpoint of the interval.
    t_mid = (level["t_m_start"] + level["t_m_end"]) / 2
    corrupted = []
    reset_done = False
    for s in all_samples:
        if (s["metric"] == "hardware_rapl_energy_joules_total"
                and s["_labels"].get("name") == "package-0"
                and not reset_done
                and s["timestamp"] >= t_mid):
            new_s = copy.deepcopy(s)
            new_s["value"] = 0.0
            corrupted.append(new_s)
            reset_done = True
        else:
            corrupted.append(s)
    return corrupted, reset_done


def fault_non_stationary(all_samples, levels, level, donor_lambda):
    # Replace the second half of one level's energy samples with another level's.
    donor = pick_target_level(levels, donor_lambda)
    if not donor:
        return all_samples, False

    t_mid_target = (level["t_m_start"] + level["t_m_end"]) / 2
    t_mid_donor = (donor["t_m_start"] + donor["t_m_end"]) / 2

    donor_pkg = sorted(
        [s for s in all_samples
         if s["metric"] == "hardware_rapl_energy_joules_total"
         and s["_labels"].get("name") == "package-0"
         and s["timestamp"] >= t_mid_donor
         and s["timestamp"] <= donor["t_m_end"] + CADENCE_S],
        key=lambda s: s["timestamp"],
    )

    target_second_half = sorted(
        [s for s in all_samples
         if s["metric"] == "hardware_rapl_energy_joules_total"
         and s["_labels"].get("name") == "package-0"
         and s["timestamp"] >= t_mid_target
         and s["timestamp"] <= level["t_m_end"] + CADENCE_S],
        key=lambda s: s["timestamp"],
    )

    if not donor_pkg or not target_second_half:
        return all_samples, False

    target_ts = set(s["timestamp"] for s in target_second_half)
    corrupted = [s for s in all_samples if s["timestamp"] not in target_ts
                 or s["metric"] != "hardware_rapl_energy_joules_total"
                 or s["_labels"].get("name") != "package-0"]

    offset = target_second_half[0]["timestamp"] - donor_pkg[0]["timestamp"]
    value_offset = target_second_half[0]["value"] - donor_pkg[0]["value"]
    n_spliced = min(len(donor_pkg), len(target_second_half))
    for i in range(n_spliced):
        new_s = copy.deepcopy(donor_pkg[i])
        new_s["timestamp"] = donor_pkg[i]["timestamp"] + offset
        new_s["value"] = donor_pkg[i]["value"] + value_offset
        corrupted.append(new_s)

    corrupted.sort(key=lambda s: s["timestamp"])
    return corrupted, True


def run_one_campaign(name, directory):
    print(f"\n{'=' * 70}")
    print(f"  Fault injections: {name}")
    print(f"{'=' * 70}")

    levels = load_levels(directory / "levels.jsonl")
    all_samples = load_samples(directory)

    if "nginx" in name:
        target_lambda = 5000.0
        nonstat_target = 1000.0
        nonstat_donor = 10000.0
        absent_mode = "softirq"
        unit = "req/s"
    else:
        target_lambda = 50.0
        nonstat_target = 50.0
        nonstat_donor = 100.0
        absent_mode = "softirq"
        unit = "%"

    target = pick_target_level(levels, target_lambda)
    if not target:
        print(f"  No load level at λ={target_lambda}, skipping")
        return

    clean_power = compute_power(all_samples, target["t_m_start"], target["t_m_end"])[0]
    clean_busy, _ = compute_busy_cores(
        all_samples, target["t_m_start"], target["t_m_end"])
    clean_checks = run_level_checks(all_samples, target["t_m_start"], target["t_m_end"])

    print(f"\n  Target level: λ = {target_lambda} {unit}")
    print(f"  Clean result: P = {clean_power:.4f} W, busy = {clean_busy:.4f} cores")
    print(f"  Clean checks: {' | '.join(k + '=' + ('pass' if v else 'FAIL') for k, v in clean_checks.items() if not k.startswith(('split_half_d', 'edge_offset_s', 'edge_offset_e')))}")

    # --- Fault 1: absent series ---
    print(f"\n  ── Fault 1: absent series ('{absent_mode}' mode removed) ──")
    f1_samples, n_removed = fault_absent_series(all_samples, target, mode=absent_mode)
    f1_power = compute_power(f1_samples, target["t_m_start"], target["t_m_end"])[0]
    f1_busy, f1_all_modes = compute_busy_cores(
        f1_samples, target["t_m_start"], target["t_m_end"])

    detected_by = []
    f1_checks = run_level_checks(f1_samples, target["t_m_start"], target["t_m_end"])
    if not f1_checks["series_constant"]:
        detected_by.append("series_constant")
    if not f1_all_modes:
        detected_by.append("all_busy_modes_present")

    busy_error = f1_busy - clean_busy
    print(f"  Samples removed: {n_removed}")
    print(f"  Detected: {'YES' if detected_by else 'NO'} ({', '.join(detected_by) if detected_by else 'none'})")
    print(f"  Energy unchanged: P = {f1_power:.4f} W (CPU modes do not affect RAPL)")
    print(f"  CPU effect: busy = {f1_busy:.4f} cores (was {clean_busy:.4f}, "
          f"error = {busy_error:+.4f} cores)")
    if clean_busy > 0:
        print(f"  Relative CPU error: {busy_error / clean_busy * 100:+.1f}%")

    # --- Fault 2: counter reset (energy zeroed mid-interval) ---
    print(f"\n  ── Fault 2: energy counter reset at interval midpoint ──")
    f2_samples, reset_ok = fault_counter_reset(all_samples, target)
    f2_power = compute_power(f2_samples, target["t_m_start"], target["t_m_end"])[0]
    f2_checks = run_level_checks(f2_samples, target["t_m_start"], target["t_m_end"])

    detected_by = []
    if not f2_checks["no_reset"]:
        detected_by.append("no_reset")
    if not f2_checks["split_half"]:
        detected_by.append("split_half")

    power_error = f2_power - clean_power
    print(f"  Reset injected: {'YES' if reset_ok else 'NO'}")
    print(f"  Detected: {'YES' if detected_by else 'NO'} ({', '.join(detected_by) if detected_by else 'none'})")
    print(f"  Corrupted result: P = {f2_power:.4f} W (was {clean_power:.4f}, "
          f"error = {power_error:+.4f} W)")
    if clean_power > 0:
        print(f"  Relative energy error: {power_error / clean_power * 100:+.1f}%")

    # --- Fault 3: non-stationary (splice second half from different level) ---
    nonstat_level = pick_target_level(levels, nonstat_target)
    if nonstat_level is None:
        nonstat_level = target
    f3_clean_power = compute_power(
        all_samples, nonstat_level["t_m_start"], nonstat_level["t_m_end"])[0]
    print(f"\n  ── Fault 3: non-stationary level ──")
    print(f"  Target: λ = {nonstat_target} {unit} (P = {f3_clean_power:.4f} W), "
          f"donor: λ = {nonstat_donor} {unit}")
    f3_samples, _ = fault_non_stationary(
        all_samples, levels, nonstat_level, nonstat_donor)
    f3_power = compute_power(
        f3_samples, nonstat_level["t_m_start"], nonstat_level["t_m_end"])[0]
    f3_checks = run_level_checks(
        f3_samples, nonstat_level["t_m_start"], nonstat_level["t_m_end"])

    detected_by = []
    if not f3_checks["split_half"]:
        detected_by.append("split_half")
    if not f3_checks["no_reset"]:
        detected_by.append("no_reset")

    power_error = f3_power - f3_clean_power
    print(f"  Detected: {'YES' if detected_by else 'NO'} ({', '.join(detected_by) if detected_by else 'none'})")
    print(f"  Corrupted result: P = {f3_power:.4f} W (was {f3_clean_power:.4f}, "
          f"error = {power_error:+.4f} W)")
    if f3_clean_power > 0:
        print(f"  Relative energy error: {power_error / f3_clean_power * 100:+.1f}%")


for name, directory in CAMPAIGNS:
    run_one_campaign(name, directory)
