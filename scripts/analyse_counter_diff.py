"""RQ3: Counter-difference validity — exact delta vs interpolated rate.

Usage:
    python3 -I scripts/analyse_counter_diff.py

Slides a 180 s sub-window across the 300 s measurement phase so edges
never leave the declared interval. Shows that exact counter deltas
(snap to real samples) are stable under window-edge shifts, while
interpolated rates vary systematically.
Generates figures/counter-diff-validity.png.
"""

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analyse_levels import (load_levels, load_samples_grouped, snap_edges,
                            unwrap_energy, CADENCE_S)

ROOT = pathlib.Path(__file__).parent.parent
FIGURES = ROOT / "figures"
FIGURES.mkdir(exist_ok=True)

CAMPAIGNS = [
    ("nginx-rerun2", ROOT / "data" / "nginx-rerun2", "req/s"),
    ("stressng-sweep-05", ROOT / "data" / "stressng-sweep-05", "%"),
]

TITLES = {
    "nginx-rerun2": "nginx (request-driven)",
    "stressng-sweep-05": "stress-ng (CPU load sweep)",
}

SUBWINDOW_S = 180.0


def interpolate_at(times, values, t):
    """Linearly interpolate value at time t from sorted (times, values)."""
    if t <= times[0]:
        return values[0]
    if t >= times[-1]:
        return values[-1]
    for i in range(len(times) - 1):
        if times[i] <= t <= times[i + 1]:
            dt = times[i + 1] - times[i]
            if dt == 0:
                return values[i]
            frac = (t - times[i]) / dt
            return values[i] + frac * (values[i + 1] - values[i])
    return values[-1]


def compute_power_exact(pkg_samples, t_start, t_end):
    """Exact delta: snap to real samples, difference unwrapped counters."""
    first, last = snap_edges(pkg_samples, t_start, t_end)
    if not first or not last or first is last:
        return float("nan")
    in_interval = [s for s in pkg_samples
                   if first["timestamp"] <= s["timestamp"] <= last["timestamp"]]
    unwrapped, _ = unwrap_energy(in_interval)
    if len(unwrapped) < 2:
        return float("nan")
    energy = unwrapped[-1] - unwrapped[0]
    elapsed = last["timestamp"] - first["timestamp"]
    return energy / elapsed


def compute_power_interpolated(times, unwrapped, t_start, t_end):
    """Interpolated rate: interpolate at declared edges, divide by declared duration."""
    v_start = interpolate_at(times, unwrapped, t_start)
    v_end = interpolate_at(times, unwrapped, t_end)
    duration = t_end - t_start
    if duration <= 0:
        return float("nan")
    return (v_end - v_start) / duration


def analyse_campaign(name, directory, unit):
    directory = pathlib.Path(directory)
    levels = load_levels(directory / "levels.jsonl")
    print(f"Loading samples from {directory}...")
    by_metric = load_samples_grouped(directory)

    results = []

    for level in levels:
        lam = level["lambda"]
        kind = level["kind"]
        t_m_start = level["t_m_start"]
        t_m_end = level["t_m_end"]
        t_m_duration = t_m_end - t_m_start
        max_shift = t_m_duration - SUBWINDOW_S

        if max_shift < 1:
            print(f"  λ={lam:8.1f}  SKIP (measurement window too short for {SUBWINDOW_S}s sub-window)")
            continue

        shifts = list(range(0, int(max_shift) + 1))

        t_wide_start = t_m_start - 2 * CADENCE_S
        t_wide_end = t_m_end + 2 * CADENCE_S

        pkg_samples = [r for r in by_metric.get("hardware_rapl_energy_joules_total", [])
                       if r["_labels"].get("name") == "package-0"
                       and t_wide_start <= r["timestamp"] <= t_wide_end]
        pkg_samples.sort(key=lambda r: r["timestamp"])

        if len(pkg_samples) < 3:
            continue

        times = [s["timestamp"] for s in pkg_samples]
        unwrapped, _ = unwrap_energy(pkg_samples)

        exact_powers = []
        interp_powers = []

        for shift in shifts:
            t_start = t_m_start + shift
            t_end = t_start + SUBWINDOW_S

            exact_powers.append(compute_power_exact(pkg_samples, t_start, t_end))
            interp_powers.append(
                compute_power_interpolated(times, unwrapped, t_start, t_end))

        exact_valid = [p for p in exact_powers if not math.isnan(p)]
        interp_valid = [p for p in interp_powers if not math.isnan(p)]

        exact_spread = max(exact_valid) - min(exact_valid) if exact_valid else 0
        interp_spread = max(interp_valid) - min(interp_valid) if interp_valid else 0

        results.append({
            "lambda": lam,
            "kind": kind,
            "shifts": shifts,
            "exact_powers": exact_powers,
            "interp_powers": interp_powers,
            "exact_spread": exact_spread,
            "interp_spread": interp_spread,
        })

        print(f"  λ={lam:8.1f}  exact spread={exact_spread:.4f} W  "
              f"interp spread={interp_spread:.4f} W  "
              f"({len(shifts)} positions)")

    return results


all_data = {}
for name, directory, unit in CAMPAIGNS:
    results = analyse_campaign(name, directory, unit)
    all_data[name] = results

# Figure: 2x2 grid of selected levels
STEP = 5  # plot every 5th point for cleaner lines

SHOW_LEVELS = {
    "nginx-rerun2": [5000, 10000],
    "stressng-sweep-05": [100, 30],
}

fig, axes = plt.subplots(2, 2, figsize=(8, 6))

for idx, (name, directory, unit) in enumerate(CAMPAIGNS):
    results = all_data[name]
    by_lam = {r["lambda"]: r for r in results}

    for col, lam in enumerate(SHOW_LEVELS[name]):
        ax = axes[idx, col]
        r = by_lam[float(lam)]
        shifts = r["shifts"][::STEP]
        exact = r["exact_powers"][::STEP]
        interp = r["interp_powers"][::STEP]

        ax.plot(shifts, exact, "-", color="#1f77b4",
                linewidth=1.2, label="Exact delta")
        ax.plot(shifts, interp, "-", color="#ff7f0e",
                linewidth=1.0, alpha=0.8, label="Interpolated")

        ax.set_xlabel("Sub-window offset (s)", fontsize=9)
        ax.set_ylabel("Package power (W)", fontsize=9)
        lam_str = f"{lam:.0f}" if lam == int(lam) else f"{lam}"
        ax.set_title(f"{TITLES[name]}, λ={lam_str} {unit}", fontsize=9)
        ax.tick_params(labelsize=8)
        ax.legend(fontsize=7)

fig.tight_layout()
out = FIGURES / "counter-diff-validity.png"
fig.savefig(out, bbox_inches="tight", dpi=300)
print(f"\nSaved {out}")

# Spread summary
print(f"\nSpread summary (180 s sub-window, max - min across all positions)")
for name, _, unit in CAMPAIGNS:
    print(f"\n{name}:")
    print(f"  {'λ':>8s}  {'Exact (W)':>10s}  {'Interp (W)':>11s}  {'Ratio':>8s}")
    print(f"  {'─' * 8}  {'─' * 10}  {'─' * 11}  {'─' * 8}")
    for r in all_data[name]:
        if r["exact_spread"] > 0:
            ratio = f"{r['interp_spread'] / r['exact_spread']:.1f}x"
        elif r["interp_spread"] > 0:
            ratio = "∞"
        else:
            ratio = "—"
        print(f"  {r['lambda']:8.1f}  {r['exact_spread']:10.4f}  "
              f"{r['interp_spread']:11.4f}  {ratio:>8s}")
