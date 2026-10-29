"""RQ2: Functional form — linear-in-λ vs linear-in-log1p(λ) vs piecewise.

Usage:
    python3 -I scripts/analyse_functional_form.py

Reads level-results.csv from both campaigns.
Prints comparison tables for energy and CPU.
Generates figures/functional-form.png (2x2: energy vs CPU, nginx vs stress-ng).
"""

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analyse_idle_conventions import load_results, ols, r_squared

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


def compute_rss(xs, ys, a, b):
    """Residual sum of squares."""
    return sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))


def aic(n, rss_val, k=2):
    """AIC = n·ln(RSS/n) + 2k."""
    if rss_val <= 0:
        return float("-inf")
    return n * math.log(rss_val / n) + 2 * k


def find_best_piecewise(lambdas, powers):
    """Try all candidate breakpoints, return the one with minimum total RSS.
    Each segment must have at least 3 points."""
    pairs = list(zip(lambdas, powers))
    unique_lambdas = sorted(set(lambdas))

    best_rss = float("inf")
    best_bp = None
    best_left = None
    best_right = None

    for bp in unique_lambdas:
        left = [(x, y) for x, y in pairs if x <= bp]
        right = [(x, y) for x, y in pairs if x > bp]
        if len(left) < 3 or len(right) < 3:
            continue

        lx, ly = zip(*left)
        rx, ry = zip(*right)
        a_l, b_l = ols(list(lx), list(ly))
        a_r, b_r = ols(list(rx), list(ry))
        rss_total = (compute_rss(list(lx), list(ly), a_l, b_l)
                     + compute_rss(list(rx), list(ry), a_r, b_r))

        if rss_total < best_rss:
            best_rss = rss_total
            best_bp = bp
            best_left = (a_l, b_l)
            best_right = (a_r, b_r)

    return best_bp, best_left, best_right, best_rss


def fit_response(lambdas, ys, y_label, name, unit):
    """Fit all three forms to a response variable. Returns fits dict and prints table."""
    log1p_lambdas = [math.log1p(l) for l in lambdas]
    n = len(lambdas)

    a_lin, b_lin = ols(lambdas, ys)
    r2_lin = r_squared(lambdas, ys, a_lin, b_lin)
    rss_lin = compute_rss(lambdas, ys, a_lin, b_lin)
    aic_lin = aic(n, rss_lin)

    a_log, b_log = ols(log1p_lambdas, ys)
    r2_log = r_squared(log1p_lambdas, ys, a_log, b_log)
    rss_log = compute_rss(log1p_lambdas, ys, a_log, b_log)
    aic_log = aic(n, rss_log)

    bp, pw_left, pw_right, rss_pw = find_best_piecewise(lambdas, ys)
    my = sum(ys) / n
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2_pw = 1 - rss_pw / ss_tot if ss_tot > 0 else 0.0
    aic_pw = aic(n, rss_pw, k=5)

    aics = {"linear": aic_lin, "log1p": aic_log, "piecewise": aic_pw}
    best_name = min(aics, key=aics.get)
    sorted_aics = sorted(aics.items(), key=lambda x: x[1])
    delta_12 = sorted_aics[1][1] - sorted_aics[0][1]
    if delta_12 < 2:
        verdict = f"INCONCLUSIVE ({sorted_aics[0][0]} vs {sorted_aics[1][0]})"
    else:
        verdict = f"{best_name} preferred"

    print(f"\n  ── {y_label} ──")
    print(f"  {'Form':<22s} {'a':>10s} {'b':>12s} {'R²':>8s} {'AIC':>8s}")
    print(f"  {'-' * 22} {'-' * 10} {'-' * 12} {'-' * 8} {'-' * 8}")
    print(f"  {'Linear in λ':<22s} {a_lin:10.4f} {b_lin:12.6f} "
          f"{r2_lin:8.5f} {aic_lin:8.2f}")
    print(f"  {'Linear in log1p(λ)':<22s} {a_log:10.4f} {b_log:12.4f} "
          f"{r2_log:8.5f} {aic_log:8.2f}")
    print(f"  {'Piecewise':<22s} {'—':>10s} {'—':>12s} "
          f"{r2_pw:8.5f} {aic_pw:8.2f}")
    if bp is not None:
        print(f"    breakpoint at λ = {bp}  "
              f"left: a={pw_left[0]:.4f} b={pw_left[1]:.6f}  "
              f"right: a={pw_right[0]:.4f} b={pw_right[1]:.6f}")
    print(f"  Verdict: {verdict}")

    return {
        "linear": (a_lin, b_lin, r2_lin),
        "log1p": (a_log, b_log, r2_log),
        "piecewise": (bp, pw_left, pw_right, r2_pw),
        "verdict": verdict,
    }


def analyse_campaign(name, directory, unit):
    levels = load_results(directory / "level-results.csv")

    lambdas = [l["lambda"] for l in levels]
    powers = [l["power_w"] for l in levels]
    cpus = [l["busy_cores"] for l in levels]

    idle_powers = [l["power_w"] for l in levels if l["kind"] == "active-idle"]
    idle_mean = sum(idle_powers) / len(idle_powers)
    idle_cpus = [l["busy_cores"] for l in levels if l["kind"] == "active-idle"]
    idle_cpu_mean = sum(idle_cpus) / len(idle_cpus)

    print(f"\n{'=' * 70}")
    print(f"  {name}  (λ unit: {unit})")
    print(f"  Measured idle mean: {idle_mean:.3f} W,  {idle_cpu_mean:.4f} cores")
    print(f"{'=' * 70}")

    energy_fits = fit_response(lambdas, powers, "Energy (W)", name, unit)
    cpu_fits = fit_response(lambdas, cpus, "CPU (busy cores)", name, unit)

    return levels, energy_fits, cpu_fits


all_data = {}
for name, directory, unit in CAMPAIGNS:
    levels, energy_fits, cpu_fits = analyse_campaign(name, directory, unit)
    all_data[name] = (levels, energy_fits, cpu_fits, unit)


def plot_fits(ax, levels, fits, x_unit, y_key, y_label):
    idle_x = [l["lambda"] for l in levels if l["kind"] == "active-idle"]
    idle_y = [l[y_key] for l in levels if l["kind"] == "active-idle"]
    load_x = [l["lambda"] for l in levels if l["kind"] == "load"]
    load_y = [l[y_key] for l in levels if l["kind"] == "load"]

    ax.scatter(idle_x, idle_y, color="black", marker="s", s=20, zorder=5, label="Idle")
    ax.scatter(load_x, load_y, color="black", marker="o", s=20, zorder=5, label="Load")

    x_max = max(l["lambda"] for l in levels)
    x_dense = [x_max * i / 200 for i in range(201)]

    a_lin, b_lin, r2_lin = fits["linear"]
    y_lin = [a_lin + b_lin * x for x in x_dense]
    ax.plot(x_dense, y_lin, color="#1f77b4", linestyle="-", linewidth=1.2,
            label=f"Linear (R²={r2_lin:.3f})")

    a_log, b_log, r2_log = fits["log1p"]
    y_log = [a_log + b_log * math.log1p(x) for x in x_dense]
    ax.plot(x_dense, y_log, color="#ff7f0e", linestyle="--", linewidth=1.2,
            label=f"log1p (R²={r2_log:.3f})")

    bp, pw_left, pw_right, r2_pw = fits["piecewise"]
    if bp is not None:
        x_left = [x for x in x_dense if x <= bp]
        x_right = [x for x in x_dense if x > bp]
        y_left = [pw_left[0] + pw_left[1] * x for x in x_left]
        y_right = [pw_right[0] + pw_right[1] * x for x in x_right]
        ax.plot(x_left, y_left, color="#2ca02c", linestyle="-.", linewidth=1.2,
                label=f"Piecewise (R²={r2_pw:.3f})")
        ax.plot(x_right, y_right, color="#2ca02c", linestyle="-.", linewidth=1.2)
        ax.axvline(bp, color="#2ca02c", linestyle=":", linewidth=0.6, alpha=0.5)

    ax.set_xlabel(f"λ ({x_unit})", fontsize=9)
    ax.set_ylabel(y_label, fontsize=9)
    ax.tick_params(labelsize=8)
    ax.legend(fontsize=6, loc="lower right")


# Figure: 2x2 grid (energy and CPU for each campaign)
fig, axes = plt.subplots(2, 2, figsize=(8, 6))

for idx, (name, directory, unit) in enumerate(CAMPAIGNS):
    levels, energy_fits, cpu_fits, _ = all_data[name]

    ax_energy = axes[idx, 0]
    ax_cpu = axes[idx, 1]

    plot_fits(ax_energy, levels, energy_fits, unit, "power_w", "Package power (W)")
    plot_fits(ax_cpu, levels, cpu_fits, unit, "busy_cores", "Busy cores")

    ax_energy.set_title(f"{TITLES[name]} — Energy", fontsize=9)
    ax_cpu.set_title(f"{TITLES[name]} — CPU", fontsize=9)

fig.tight_layout()
out = FIGURES / "functional-form.png"
fig.savefig(out, bbox_inches="tight", dpi=300)
print(f"\nSaved {out}")
