"""RQ1: Compare four idle-floor conventions on both campaigns.

Usage:
    python3 -I scripts/analyse_idle_conventions.py

Reads level-results.csv from data/nginx-rerun2 and data/stressng-sweep-05.
Prints comparison tables. Generates figures/idle-conventions.png.
"""

import csv
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).parent.parent
FIGURES = ROOT / "figures"
FIGURES.mkdir(exist_ok=True)

CAMPAIGNS = [
    ("nginx-rerun2", ROOT / "data" / "nginx-rerun2", "req/s"),
    ("stressng-sweep-05", ROOT / "data" / "stressng-sweep-05", "%"),
]


def load_results(path):
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append({
                "position": int(row["position"]),
                "lambda": float(row["lambda"]),
                "kind": row["kind"],
                "power_w": float(row["power_w"]),
                "busy_cores": float(row["busy_cores"]),
            })
    return rows


def ols(xs, ys):
    # y = a + b*x. Returns (a, b)
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx else 0.0
    a = my - b * mx
    return a, b


def ols_zero(xs, ys):
    # y = b*x (forced through origin). Returns b
    num = sum(x * y for x, y in zip(xs, ys))
    den = sum(x * x for x in xs)
    return num / den if den else 0.0


def r_squared(xs, ys, a, b):
    # R-squared of y = a + b*x evaluated on the given pairs
    my = sum(ys) / len(ys)
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return 1 - ss_res / ss_tot if ss_tot > 0 else 0.0


def analyse_campaign(name, directory, unit):
    levels = load_results(directory / "level-results.csv")
    all_x = [l["lambda"] for l in levels]
    all_y = [l["power_w"] for l in levels]

    idles = [l for l in levels if l["kind"] == "active-idle"]
    loads = [l for l in levels if l["kind"] == "load"]
    load_x = [l["lambda"] for l in loads]
    load_y = [l["power_w"] for l in loads]

    idle_powers = [l["power_w"] for l in idles]
    idle_mean = sum(idle_powers) / len(idle_powers)
    idle_spread = max(idle_powers) - min(idle_powers)

    results = []

    # Convention 1: Measured levels — all points in one fit
    a1, b1 = ols(all_x, all_y)
    r2_1 = r_squared(all_x, all_y, a1, b1)
    results.append(("Measured levels", a1, b1, r2_1))

    # Convention 2: Bracketed — nearest idle before+after each load level
    by_pos = sorted(levels, key=lambda l: l["position"])
    idle_pos = [(l["position"], l["power_w"]) for l in by_pos if l["kind"] == "active-idle"]

    bracket_lambdas = []
    bracket_gaps = []
    for ll in sorted(loads, key=lambda l: l["position"]):
        pos = ll["position"]
        before = [(p, pw) for p, pw in idle_pos if p < pos]
        after = [(p, pw) for p, pw in idle_pos if p > pos]
        bpowers = []
        if before:
            bpowers.append(max(before, key=lambda x: x[0])[1])
        if after:
            bpowers.append(min(after, key=lambda x: x[0])[1])
        bracket = sum(bpowers) / len(bpowers)
        bracket_lambdas.append(ll["lambda"])
        bracket_gaps.append(ll["power_w"] - bracket)

    b2 = ols_zero(bracket_lambdas, bracket_gaps)
    a2 = idle_mean
    r2_2 = r_squared(all_x, all_y, a2, b2)
    results.append(("Bracketed", a2, b2, r2_2))

    # Convention 3: Subtracted — global mean idle subtracted, fit through zero
    a3 = idle_mean
    gaps3 = [l["power_w"] - a3 for l in loads]
    b3 = ols_zero(load_x, gaps3)
    r2_3 = r_squared(all_x, all_y, a3, b3)
    results.append(("Subtracted", a3, b3, r2_3))

    # Convention 4: Extrapolated — fit only load levels
    a4, b4 = ols(load_x, load_y)
    r2_4 = r_squared(all_x, all_y, a4, b4)
    results.append(("Extrapolated", a4, b4, r2_4))

    # Print
    print(f"\n{'=' * 70}")
    print(f"  {name}  (λ unit: {unit})")
    print(f"  Idle levels: {len(idles)},  mean = {idle_mean:.3f} W,  "
          f"spread = {idle_spread:.3f} W")
    print(f"{'=' * 70}")
    print(f"  {'Convention':<20s} {'a (W)':>8s} {'b (W/unit)':>12s} "
          f"{'R²':>8s}  {'Δa (W)':>8s}  {'Δb':>8s}")
    print(f"  {'-' * 20} {'-' * 8} {'-' * 12} {'-' * 8}  {'-' * 8}  {'-' * 8}")
    for rname, a, b, r2 in results:
        da = a - a1
        db_pct = (b - b1) / b1 * 100 if b1 != 0 else 0.0
        print(f"  {rname:<20s} {a:8.4f} {b:12.6f} "
              f"{r2:8.5f}  {da:+8.4f}  {db_pct:+7.2f}%")

    return levels, results


def main():
    all_campaign_data = {}
    for name, directory, unit in CAMPAIGNS:
        levels, results = analyse_campaign(name, directory, unit)
        all_campaign_data[name] = (levels, results, unit)

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]
    styles = ["-", "--", "-.", ":"]

    fig, axes = plt.subplots(1, 2, figsize=(8, 3.5))

    for idx, (name, directory, unit) in enumerate(CAMPAIGNS):
        ax = axes[idx]
        levels, results, _ = all_campaign_data[name]

        idle_x = [l["lambda"] for l in levels if l["kind"] == "active-idle"]
        idle_y = [l["power_w"] for l in levels if l["kind"] == "active-idle"]
        load_x = [l["lambda"] for l in levels if l["kind"] == "load"]
        load_y = [l["power_w"] for l in levels if l["kind"] == "load"]

        ax.scatter(idle_x, idle_y, color="black", marker="s", s=30, zorder=5, label="Idle")
        ax.scatter(load_x, load_y, color="black", marker="o", s=30, zorder=5, label="Load")

        x_max = max(l["lambda"] for l in levels)
        x_line = [0, x_max]
        for i, (rname, a, b, r2) in enumerate(results):
            y_line = [a + b * x for x in x_line]
            ax.plot(x_line, y_line, color=colors[i], linestyle=styles[i],
                    linewidth=1.2, label=rname)

        ax.set_xlabel(f"λ ({unit})", fontsize=9)
        ax.set_ylabel("Package power (W)", fontsize=9)
        titles = {"nginx-rerun2": "nginx (request-driven)",
                  "stressng-sweep-05": "stress-ng (CPU load sweep)"}
        ax.set_title(titles.get(name, name), fontsize=9)
        ax.tick_params(labelsize=8)
        ax.legend(fontsize=7, loc="lower right")

    fig.tight_layout()
    out = FIGURES / "idle-conventions.png"
    fig.savefig(out, bbox_inches="tight", dpi=300)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
