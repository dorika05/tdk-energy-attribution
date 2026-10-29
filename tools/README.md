# tools/

First code in this repo, which was docs-only until 2026-10-07. **The layout is an open decision** — these seven files are placed together because they share one module, not because `tools/` is the chosen structure.

Written against the inHouse cluster for the TDK campaign. Platform specifics (metric names, node labels, the RAPL counter range) are declared in constants at the top of each file with the measurement that produced them, so they can be re-derived elsewhere rather than silently inherited.

| file | does |
|---|---|
| `promsamples.py` | the one definition site for "how a sample is obtained" from Prometheus. Range-vector selector, chunked, true sample timestamps |
| `runmeta.py` | run identity (`experiment_id` / `run_id`), UTC timestamp formatting, and optional Grafana annotations |
| `measure_cadence.py` | measures the real scrape cadence per metric, then derives the `t_m` floor and the aliasing band from it |
| `archive_samples.py` | writes raw per-sample data to long-format CSV with a presence/fault manifest beside it |
| `run_sweep.py` | drives one load sweep and records the declared interval edges. Measures nothing, analyses nothing |
| `infra_status.py` | **the handover check.** One command, one verdict: is the infrastructure ready, what must the researcher use, what must she derive herself |
| `make_dashboard.py` | builds and uploads the Grafana dashboard used to supervise a sweep |

## Start here

```sh
python3 -I infra_status.py --node node2          # exit 0 READY, 1 DEGRADED, 2 BLOCKED
```

Run this after any cluster change and keep the output beside the session it
preceded. It checks only the prerequisites the infrastructure owns, prints the
measured values a campaign must use, and then names the values the researcher has
to derive herself plus the things this tooling deliberately does not provide. The
last two lists are the handover boundary: an item on them is not a gap to be filed
against the tooling.

## Running them

Prometheus is not routable from outside the cluster; reach it through a port-forward:

```sh
kubectl --context=inHouse -n monitoring port-forward \
    svc/monitoring-kube-prometheus-prometheus 19090:9090 &
```

All four default to `http://127.0.0.1:19090`. Run them with `python3 -I` so nothing is imported from whatever directory you are standing in; each adds its own directory back to `sys.path` explicitly so the shared module still resolves. Standard library only.

```sh
python3 -I measure_cadence.py --hours 2 --node node2 --json cadence.json

python3 -I run_sweep.py --node node2 --experiment-id cdn-baseline \
    --cadence-json cadence.json --rates 0,1000,8000,16000,32000,48000 \
    --out ./session-01 --seed 42

python3 -I archive_samples.py --session ./session-01/session.json
```

The archiver takes the node, both ids and the time window from `session.json`, so
none of them is retyped. The sweep's archive window carries a margin past the last
level, so if you archive the instant a sweep ends the trailing margin has not been
scraped yet: the tool clamps to now, says so, and tells you when to re-run.

## Identity, and why it is on every row

`experiment_id` names the campaign or configuration -- whatever is held fixed --
and `run_id` names one sweep inside it, carrying its own seed
(`cdn-baseline.20261007T124316Z.s7`) so the shuffled level order can be
reconstructed from the name. Both appear in `session.json`, in every
`levels.jsonl` record, in the manifest, and as the first two **columns of every
CSV row**. That is what turns "results from different configurations are not
pooled" into a filter the analysis applies rather than a rule somebody has to
remember while concatenating files.

Every timestamp is stored twice. The epoch float is authoritative and the only
thing arithmetic is done on; the ISO-8601 string beside it is UTC with an explicit
offset, for humans. Nothing computes from the string -- a naive local-time string
is ambiguous across a daylight-saving boundary, and a campaign that straddles one
would quietly acquire a one-hour error.

## Watching a sweep while it runs

Grafana is already deployed against the same Prometheus (NodePort 30300). Give the
sweep a Grafana URL and token and it posts one annotation per **measurement
interval** -- so the shaded span on a dashboard is exactly where a number will be
taken from, with the settling and drain periods visibly outside it:

```sh
export GRAFANA_URL=http://<node-ip>:30300 GRAFANA_TOKEN=<service-account-token>
python3 -I run_sweep.py ... # annotations tagged load-sweep, experiment:<id>, run:<id>
```

Annotation failures are warned about and ignored: supervision must never be able
to stop a measurement. And a number read off a dashboard is never a result -- the
panels may use `rate()`, which is precisely what the analysis refuses to use.

## Two things these tools will not do

**They never read `query_range` for measurement.** It returns values re-stamped onto the grid you asked for, not the instants the exporter was actually read at. Measured on this cluster: the same sample reported at `1791373597.998` by a range-vector selector and at `1791373600` by `query_range`, a 2.002 s displacement. Interval-edge offsets, true elapsed times and any later recomputation over different boundaries all need the real instant.

**They never report an empty result as an absent metric.** An empty result means either the metric is not exported or the selector did not match it, and those are different findings. Both tools go and look, then say which. This was not a hypothetical: filtering every family on `node` returns nothing for Kepler (`node_name`) and for node-exporter and cAdvisor (no node label at all), and the first version of `measure_cadence.py` reported two present metrics as ABSENT because of it.
