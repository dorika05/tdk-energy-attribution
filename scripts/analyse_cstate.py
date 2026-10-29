"""C-state analysis: corroborate the energy breakpoint at λ=60% in stress-ng.

Usage:
    python3 -I scripts/analyse_cstate.py

Shows that the energy breakpoint coincides with a C-state distribution
shift: idle cores move from deep C6 to shallower C3 around λ=60%.
CPU time stays perfectly linear throughout — the break is in power
management, not in utilisation.

Xeon D-1541 (Broadwell-DE) with intel_idle:
    state0 = POLL, state1 = C1, state2 = C1E, state3 = C3, state4 = C6

Generates figures/cstate-transition.png and figures/cstate-absolute.png.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analyse_levels import load_levels, load_samples_grouped, CADENCE_S
from analyse_idle_conventions import load_results

ROOT = pathlib.Path(__file__).parent.parent
FIGURES = ROOT / "figures"
FIGURES.mkdir(exist_ok=True)

CAMPAIGN_DIR = ROOT / "data" / "stressng-sweep-05"
N_CORES = 8   # Xeon D-1541: 8 physical cores
N_THREADS = 16


def analyse():
    levels = load_levels(CAMPAIGN_DIR / "levels.jsonl")
    level_results = load_results(CAMPAIGN_DIR / "level-results.csv")
    by_metric = load_samples_grouped(CAMPAIGN_DIR)

    # Index per-core CPU data by (core, mode)
    core_data = {}
    for r in by_metric.get("hardware_cpu_core_seconds_total", []):
        key = (int(r["_labels"]["core"]), r["_labels"]["mode"])
        core_data.setdefault(key, []).append(r)
    for v in core_data.values():
        v.sort(key=lambda r: r["timestamp"])

    # Index C-state data by state
    cstate_data = {}
    for r in by_metric.get("hardware_cpu_cstate_residency_microseconds_total", []):
        state = int(r["_labels"]["state"])
        cstate_data.setdefault(state, []).append(r)
    for v in cstate_data.values():
        v.sort(key=lambda r: r["timestamp"])

    # Power from level-results: average over all levels at the same lambda
    power_by_lam = {}
    power_counts = {}
    for lr in level_results:
        lam = lr["lambda"]
        power_by_lam[lam] = power_by_lam.get(lam, 0) + lr["power_w"]
        power_counts[lam] = power_counts.get(lam, 0) + 1
    for lam in power_by_lam:
        power_by_lam[lam] /= power_counts[lam]

    # Collect per-level data, then average duplicates (multiple idle levels)
    def analyse_level(level):
        t_start = level["t_m_start"]
        t_end = level["t_m_end"]

        busy_fracs = []
        for core in range(N_THREADS):
            total_delta = 0
            idle_delta = 0
            for mode in ["idle", "iowait", "irq", "nice", "softirq",
                         "steal", "system", "user"]:
                samples = core_data.get((core, mode), [])
                window = [s for s in samples
                          if t_start <= s["timestamp"] <= t_end]
                if len(window) >= 2:
                    delta = window[-1]["value"] - window[0]["value"]
                    total_delta += delta
                    if mode == "idle":
                        idle_delta = delta
            if total_delta > 0:
                busy_fracs.append(1 - idle_delta / total_delta)

        mean_busy = sum(busy_fracs) / len(busy_fracs) if busy_fracs else 0

        cs_deltas = {}
        for state, samples in cstate_data.items():
            window = [s for s in samples
                      if t_start <= s["timestamp"] <= t_end]
            if len(window) >= 2:
                cs_deltas[state] = window[-1]["value"] - window[0]["value"]

        return mean_busy, cs_deltas

    # Group levels by lambda, analyse each, average idle levels
    from collections import defaultdict
    levels_by_lam = defaultdict(list)
    for level in levels:
        levels_by_lam[level["lambda"]].append(level)

    rows = []
    for lam in sorted(levels_by_lam.keys()):
        group = levels_by_lam[lam]

        all_busy = []
        all_cs = defaultdict(float)
        for level in group:
            mean_busy, cs_deltas = analyse_level(level)
            all_busy.append(mean_busy)
            for state, delta in cs_deltas.items():
                all_cs[state] += delta

        mean_busy = sum(all_busy) / len(all_busy)
        n = len(group)
        cs_avg = {state: total / n for state, total in all_cs.items()}

        total_cs = sum(cs_avg.values())
        c6_pct = cs_avg.get(4, 0) / total_cs * 100 if total_cs > 0 else 0
        c3_pct = cs_avg.get(3, 0) / total_cs * 100 if total_cs > 0 else 0
        c1_c1e_pct = ((cs_avg.get(1, 0) + cs_avg.get(2, 0))
                      / total_cs * 100 if total_cs > 0 else 0)

        power = power_by_lam.get(lam, float("nan"))

        total_idle_s = total_cs / 1e6
        c6_abs_s = cs_avg.get(4, 0) / 1e6
        c3_abs_s = cs_avg.get(3, 0) / 1e6

        rows.append({
            "lambda": lam,
            "power_w": power,
            "mean_busy": mean_busy,
            "c6_pct": c6_pct,
            "c3_pct": c3_pct,
            "c1_c1e_pct": c1_c1e_pct,
            "total_idle_s": total_idle_s,
            "c6_abs_s": c6_abs_s,
            "c3_abs_s": c3_abs_s,
        })

    # Print table
    print(f"{'λ (%)':>6s}  {'Power (W)':>10s}  {'per-thr':>8s}  "
          f"{'C6 %':>6s}  {'C3 %':>6s}  {'C1+C1E%':>7s}  "
          f"{'idle (s)':>8s}  {'C6 (s)':>7s}  {'C3 (s)':>7s}")
    print("-" * 84)
    for r in rows:
        note = "  ← breakpoint" if r["lambda"] == 60 else ""
        print(f"{r['lambda']:6.0f}  {r['power_w']:10.2f}  {r['mean_busy']:8.3f}  "
              f"{r['c6_pct']:6.1f}  {r['c3_pct']:6.1f}  {r['c1_c1e_pct']:7.1f}  "
              f"{r['total_idle_s']:8.1f}  {r['c6_abs_s']:7.1f}  "
              f"{r['c3_abs_s']:7.1f}{note}")

    return rows


rows = analyse()

# Figure 1: relative C-state residency
lambdas = [r["lambda"] for r in rows if r["lambda"] > 0]
powers = [r["power_w"] for r in rows if r["lambda"] > 0]
c6s = [r["c6_pct"] for r in rows if r["lambda"] > 0]
c3s = [r["c3_pct"] for r in rows if r["lambda"] > 0]
busys = [r["mean_busy"] for r in rows if r["lambda"] > 0]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 3.5))

# Left panel: energy + C6% on dual y-axis
color_e = "#1f77b4"
color_c = "#2ca02c"
ax1.plot(lambdas, powers, "-o", color=color_e, markersize=4, linewidth=1.2,
         label="Package power")
ax1.set_xlabel("λ (%)", fontsize=9)
ax1.set_ylabel("Package power (W)", fontsize=9, color=color_e)
ax1.tick_params(axis="y", labelcolor=color_e, labelsize=8)
ax1.tick_params(axis="x", labelsize=8)
ax1.axvline(60, color="gray", linestyle=":", linewidth=0.8, alpha=0.6)

ax1b = ax1.twinx()
ax1b.plot(lambdas, c6s, "--s", color=color_c, markersize=4, linewidth=1.2,
          label="C6 residency")
ax1b.set_ylabel("C6 residency (%)", fontsize=9, color=color_c)
ax1b.tick_params(axis="y", labelcolor=color_c, labelsize=8)

lines1 = ax1.get_lines() + ax1b.get_lines()
ax1.legend([l for l in lines1 if not l.get_label().startswith("_")],
           [l.get_label() for l in lines1 if not l.get_label().startswith("_")],
           fontsize=7, loc="center left")
ax1.set_title("Energy vs C-state depth (C6 = deepest)", fontsize=9)

# Right panel: CPU busy cores (linear) vs energy (piecewise)
color_cpu = "#d62728"
ax2.plot(lambdas, powers, "-o", color=color_e, markersize=4, linewidth=1.2,
         label="Package power")
ax2.set_xlabel("λ (%)", fontsize=9)
ax2.set_ylabel("Package power (W)", fontsize=9, color=color_e)
ax2.tick_params(axis="y", labelcolor=color_e, labelsize=8)
ax2.tick_params(axis="x", labelsize=8)
ax2.axvline(60, color="gray", linestyle=":", linewidth=0.8, alpha=0.6)

ax2b = ax2.twinx()
ax2b.plot(lambdas, busys, "--^", color=color_cpu, markersize=4, linewidth=1.2,
          label="Per-thread busy")
ax2b.set_ylabel("Per-thread busy fraction", fontsize=9, color=color_cpu)
ax2b.tick_params(axis="y", labelcolor=color_cpu, labelsize=8)

lines2 = ax2.get_lines() + ax2b.get_lines()
ax2.legend([l for l in lines2 if not l.get_label().startswith("_")],
           [l.get_label() for l in lines2 if not l.get_label().startswith("_")],
           fontsize=7, loc="center left")
ax2.set_title("Energy vs CPU utilisation", fontsize=9)

fig.tight_layout()
out = FIGURES / "cstate-transition.png"
fig.savefig(out, bbox_inches="tight", dpi=300)
print(f"\nSaved {out}")

# Figure 2: absolute C-state time
total_idles = [r["total_idle_s"] for r in rows if r["lambda"] > 0]
c6_abss = [r["c6_abs_s"] for r in rows if r["lambda"] > 0]
c3_abss = [r["c3_abs_s"] for r in rows if r["lambda"] > 0]

fig2, (ax3, ax4) = plt.subplots(1, 2, figsize=(8, 3.5))

# Left: energy + absolute C6 time
ax3.plot(lambdas, powers, "-o", color=color_e, markersize=4, linewidth=1.2,
         label="Package power")
ax3.set_xlabel("λ (%)", fontsize=9)
ax3.set_ylabel("Package power (W)", fontsize=9, color=color_e)
ax3.tick_params(axis="y", labelcolor=color_e, labelsize=8)
ax3.tick_params(axis="x", labelsize=8)
ax3.axvline(60, color="gray", linestyle=":", linewidth=0.8, alpha=0.6)

ax3b = ax3.twinx()
ax3b.plot(lambdas, c6_abss, "--s", color=color_c, markersize=4, linewidth=1.2,
          label="C6 time (abs)")
ax3b.set_ylabel("C6 residency (s)", fontsize=9, color=color_c)
ax3b.tick_params(axis="y", labelcolor=color_c, labelsize=8)

lines3 = ax3.get_lines() + ax3b.get_lines()
ax3.legend([l for l in lines3 if not l.get_label().startswith("_")],
           [l.get_label() for l in lines3 if not l.get_label().startswith("_")],
           fontsize=7, loc="center right")
ax3.set_title("Energy vs absolute C6 time", fontsize=9)

# Right: stacked absolute C-state breakdown
c1e_abss = [t - c4 - c3 for t, c4, c3
            in zip(total_idles, c6_abss, c3_abss)]
ax4.stackplot(lambdas, c6_abss, c3_abss, c1e_abss,
              labels=["C6 (deepest)", "C3", "C1+C1E (shallowest)"],
              colors=["#2ca02c", "#ff7f0e", "#d62728"], alpha=0.8)
ax4.set_xlabel("λ (%)", fontsize=9)
ax4.set_ylabel("Idle residency (s)", fontsize=9)
ax4.tick_params(labelsize=8)
ax4.axvline(60, color="gray", linestyle=":", linewidth=0.8, alpha=0.6)
ax4.legend(fontsize=7, loc="upper right")
ax4.set_title("C-state breakdown (absolute)", fontsize=9)

fig2.tight_layout()
out2 = FIGURES / "cstate-absolute.png"
fig2.savefig(out2, bbox_inches="tight", dpi=300)
print(f"Saved {out2}")
