# Plot step-response figure for the TDK paper (Section 5.4).

import csv
import pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
FIGURES = ROOT / "figures"

def read_csv(path):
    ts, ps = [], []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts.append(float(row["relative_seconds"]))
            ps.append(float(row["power_watts"]))
    return ts, ps

t_nginx, p_nginx = read_csv(HERE / "step-response-nginx-power.csv")
t_stress, p_stress = read_csv(HERE / "step-response-stressng-power.csv")

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7, 2.8), sharey=False)

# --- nginx ---
ax1.plot(t_nginx, p_nginx, linewidth=0.7, color="black")
ax1.axvspan(0, 62, alpha=0.08, color="green")
ax1.axvspan(62, 122, alpha=0.08, color="red")
ax1.axvspan(122, max(t_nginx), alpha=0.08, color="green")
ax1.axvline(62, color="gray", linestyle="--", linewidth=0.6)
ax1.axvline(122, color="gray", linestyle="--", linewidth=0.6)
ax1.set_xlabel("Time (s)", fontsize=9)
ax1.set_ylabel("Package power (W)", fontsize=9)
ax1.set_title("nginx  (10 000 req/s)", fontsize=9)
ax1.set_ylim(7, 15)
ax1.tick_params(labelsize=8)

# --- stress-ng ---
ax2.plot(t_stress, p_stress, linewidth=0.7, color="black")
ax2.axvspan(0, 61, alpha=0.08, color="green")
ax2.axvspan(61, 120, alpha=0.08, color="red")
ax2.axvspan(120, max(t_stress), alpha=0.08, color="green")
ax2.axvline(61, color="gray", linestyle="--", linewidth=0.6)
ax2.axvline(120, color="gray", linestyle="--", linewidth=0.6)
ax2.set_xlabel("Time (s)", fontsize=9)
ax2.set_ylabel("Package power (W)", fontsize=9)
ax2.set_title("stress-ng  (100% CPU)", fontsize=9)
ax2.set_ylim(7, 45)
ax2.tick_params(labelsize=8)

fig.tight_layout()
out = FIGURES / "step-response.png"
fig.savefig(out, bbox_inches="tight", dpi=300)
print(f"Saved to {out}")
