# tdk-energy-attribution

Analysis scripts and data for TDK paper: CPU energy attribution of containerized cloud applications.

## Repository structure

```
scripts/          Analysis scripts (reproduce every table and figure)
data/             Measurement data from two campaigns
manifests/        Kubernetes workload manifests
tools/            Measurement collection tooling (sweep runner, sample archiver)
```

## Data

Two campaigns, each with 13 levels (10 load + 3 idle), 300 s measurement windows:

- `data/nginx-rerun2/` — nginx (network I/O), load in req/s
- `data/stressng-sweep-05/` — stress-ng (CPU-bound), load in %

`level-results.csv` contains per-level results (power, CPU, validity checks). `samples.csv` contains raw Prometheus samples.

## Analysis scripts

All scripts take a campaign directory or its `level-results.csv` as input. Requires Python 3 with numpy, scipy, and matplotlib.

| Script | Output |
|---|---|
| `analyse_levels.py` | Per-level power, CPU, validity checks |
| `analyse_idle_conventions.py` | Idle convention comparison (measured, bracketed, subtracted, extrapolated) |
| `analyse_functional_form.py` | Linear / log1p / piecewise fits with R² and AIC |
| `analyse_counter_diff.py` | Exact delta vs interpolation spread per level |
| `analyse_cstate.py` | C-state residency per load level |
| `analyse_fault_injections.py` | Fault injection results (absent series, counter reset, non-stationarity) |
| `compute_step_response.py` | Step-response decay constants |
| `plot_step_response.py` | Step-response figure |

## How to run

```bash
# Per-level power, CPU, and validity checks
python3 scripts/analyse_levels.py data/nginx-rerun2
python3 scripts/analyse_levels.py data/stressng-sweep-05

# Idle convention comparison
python3 scripts/analyse_idle_conventions.py data/nginx-rerun2/level-results.csv
python3 scripts/analyse_idle_conventions.py data/stressng-sweep-05/level-results.csv

# Functional form comparison (linear / log1p / piecewise)
python3 scripts/analyse_functional_form.py data/nginx-rerun2/level-results.csv
python3 scripts/analyse_functional_form.py data/stressng-sweep-05/level-results.csv

# Exact delta vs interpolation spread
python3 scripts/analyse_counter_diff.py data/nginx-rerun2/level-results.csv
python3 scripts/analyse_counter_diff.py data/stressng-sweep-05/level-results.csv

# C-state residency
python3 scripts/analyse_cstate.py data/nginx-rerun2/level-results.csv
python3 scripts/analyse_cstate.py data/stressng-sweep-05/level-results.csv

# Fault injection results
python3 scripts/analyse_fault_injections.py data/nginx-rerun2/level-results.csv
python3 scripts/analyse_fault_injections.py data/stressng-sweep-05/level-results.csv

# Step-response decay constants and figure
python3 scripts/compute_step_response.py
python3 scripts/plot_step_response.py
```

All scripts print results to stdout and save figures to `figures/`.
