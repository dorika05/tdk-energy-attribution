#!/usr/bin/env python3
"""Build and upload the Grafana dashboard used to SUPERVISE a load sweep.

This dashboard is for watching a run in progress: confirming the generator is
actually driving load, that the plateau looks stationary, and that nothing odd
happened mid-level. It is NOT a source of results.

Every power and rate panel here uses rate(), which is exactly the estimator the
analysis refuses to use -- it extrapolates to unaligned window edges, and on this
cluster that was worth a factor of 3.7 on one quantity. A number read off this
dashboard is a number read off the wrong estimator. The panel titles say so.

The dashboard reads the annotations run_sweep.py posts, so each measurement
interval appears as a shaded band: settling and drain stay visibly outside it.

Usage:
    export GRAFANA_URL=http://localhost:30300 GRAFANA_TOKEN=glsa_...
    python3 -I make_dashboard.py                 # upload
    python3 -I make_dashboard.py --print         # dump JSON, upload nothing
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DS = {"type": "prometheus", "uid": "prometheus"}
WARN = "  [rate() -- supervision only, never a result]"


def target(expr, legend, ref="A"):
    return {"datasource": DS, "editorMode": "code", "expr": expr,
            "legendFormat": legend, "range": True, "refId": ref}


def panel(pid, title, x, y, w, h, targets, unit=None, desc="", stack=False,
          fill=0, points=False):
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 1,
                    "fillOpacity": fill,
                    "showPoints": "always" if points else "auto",
                    "pointSize": 4,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                },
            },
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "calcs": ["mean", "min", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def text_panel(pid, y, content):
    return {"id": pid, "type": "text", "title": "", "transparent": True,
            "gridPos": {"h": 3, "w": 24, "x": 0, "y": y},
            "options": {"mode": "markdown", "content": content}}


def build():
    n = '{node="$node"}'
    nm = 'node="$node"'
    panels = [
        text_panel(1, 0,
                   "### Load-sweep supervision — **not** a source of results\n"
                   "Every rate panel below uses `rate()`, which extrapolates to "
                   "unaligned interval edges; on this cluster that was worth a factor "
                   "of 3.7 on one quantity. Results come from counter differences over "
                   "the archived raw samples. Shaded bands are measurement intervals "
                   "posted by `run_sweep.py` — settling and drain are outside them."),

        panel(2, "RAPL package power" + WARN, 0, 3, 12, 8, [
            target(f'rate(hardware_rapl_energy_joules_total{{{nm},name="package-0"}}[1m])',
                   "package-0"),
            target(f'rate(hardware_rapl_energy_joules_total{{{nm},name="dram"}}[1m])',
                   "dram", "B"),
        ], unit="watt",
            desc="Expect a step up at each load level and a decay after it. A dip to "
                 "zero or a spike is the counter wrap, not the machine."),

        panel(3, "RAPL raw counter — watch the wrap", 12, 3, 12, 8, [
            target(f'hardware_rapl_energy_joules_total{{{nm},name="package-0"}}',
                   "package-0 joules"),
        ], unit="joule",
            desc="The sawtooth IS the counter wrapping at 262144 J, roughly every "
                 "8.3 h at idle and faster under load. Each tooth is one wrap the "
                 "analysis has to add a counter range back into."),

        panel(4, "Busy cores" + WARN, 0, 11, 12, 8, [
            target(f'sum(rate(hardware_cpu_seconds_total{{{nm},mode!="idle"}}[1m]))',
                   "busy cores"),
        ], unit="none",
            desc="Sum of every non-idle /proc/stat mode. Should be flat inside a "
                 "measurement interval; a slope means the plateau is not stationary."),

        panel(5, "CPU time by mode" + WARN, 12, 11, 12, 8, [
            target(f'sum by (mode) (rate(hardware_cpu_seconds_total{{{nm},mode!="idle"}}[1m]))',
                   "{{mode}}"),
        ], unit="none", stack=True, fill=40,
            desc="softirq growing faster than user is the network-stack work that is "
                 "charged to no container. nice must be visible, not zero."),

        panel(6, "Offered load witness — packets/s" + WARN, 0, 19, 8, 7, [
            target(f'rate(hardware_network_receive_packets_total{n}[1m])', "rx", "A"),
            target(f'rate(hardware_network_transmit_packets_total{n}[1m])', "tx", "B"),
        ], unit="pps",
            desc="Confirms the generator is actually driving the node. Compare against "
                 "the rate you asked for: a shortfall means the generator, not the "
                 "subject, is the bottleneck."),

        panel(7, "C-state residency" + WARN, 8, 19, 8, 7, [
            target(f'sum by (state) (rate(hardware_cpu_cstate_residency_microseconds_total{n}[1m])) / 1e6',
                   "state {{state}}"),
        ], unit="none",
            desc="Core-equivalents resident in each idle state. The energy-shape "
                 "mechanism predicts residency shifting from deep to shallow states "
                 "as load rises. Indices only -- the names are not exported."),

        panel(8, "NAPI budget exhaustion + throttling" + WARN, 16, 19, 8, 7, [
            target(f'sum(rate(hardware_softnet_squeezed_total{n}[1m]))', "softnet squeezed", "A"),
            target(f'sum(rate(hardware_cpu_thermal_throttle_total{n}[1m]))', "thermal throttle", "B"),
        ], unit="none",
            desc="Nonzero squeezed means the NIC poll budget is being exhausted and "
                 "work is deferring to ksoftirqd. Nonzero throttle invalidates a level."),

        panel(9, "CPU frequency" + WARN, 0, 26, 12, 7, [
            target(f'hardware_cpu_frequency_average_hertz{n} / 1e9', "GHz"),
        ], unit="none",
            desc="The governor here is schedutil, so frequency tracks load. Useful for "
                 "seeing when the settling transient has finished."),

        panel(10, "cgroup root vs /proc/stat" + WARN, 12, 26, 12, 7, [
            target(f'rate(hardware_cgroup_root_cpu_seconds_total{{{nm},mode="usage"}}[1m])',
                   "cgroup root usage", "A"),
            target(f'sum(rate(hardware_cpu_seconds_total{{{nm},mode!="idle"}}[1m]))',
                   "/proc/stat busy", "B"),
        ], unit="none",
            desc="Two reference instruments on the same quantity. They should agree "
                 "closely; a persistent gap is work the control-group hierarchy does "
                 "not see."),
    ]

    return {
        "title": "Load sweep — supervision",
        "uid": "load-sweep-supervision",
        "tags": ["load-sweep", "energy-attribution"],
        "timezone": "utc",
        "schemaVersion": 39,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "editable": True,
        "templating": {"list": [{
            "name": "node", "label": "node", "type": "query",
            "datasource": DS,
            "query": {"qryType": 1, "query": "label_values(hardware_cpu_seconds_total, node)",
                      "refId": "node"},
            "refresh": 1, "includeAll": False, "multi": False,
            "current": {"text": "node2", "value": "node2"},
        }]},
        "annotations": {"list": [
            {
                # The bands run_sweep.py posts. matchAny=false so only annotations
                # carrying the load-sweep tag appear, not every Grafana annotation.
                "name": "measurement intervals",
                "enable": True, "iconColor": "semi-dark-green",
                "type": "tags", "tags": ["load-sweep"], "matchAny": False, "limit": 200,
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "target": {"type": "tags", "tags": ["load-sweep"], "matchAny": False,
                           "limit": 200},
            },
            {
                "name": "active-idle levels",
                "enable": True, "iconColor": "semi-dark-blue",
                "type": "tags", "tags": ["active-idle"], "matchAny": False, "limit": 200,
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "target": {"type": "tags", "tags": ["active-idle"], "matchAny": False,
                           "limit": 200},
            },
        ]},
        "panels": panels,
    }


def upload(url, token, dash):
    body = {"dashboard": dash, "overwrite": True,
            "message": "generated by tools/make_dashboard.py"}
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/dashboards/db",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GRAFANA_URL"))
    ap.add_argument("--token", default=os.environ.get("GRAFANA_TOKEN"))
    ap.add_argument("--print", action="store_true", dest="dump")
    args = ap.parse_args()

    dash = build()
    if args.dump:
        print(json.dumps(dash, indent=2))
        return 0
    if not args.url or not args.token:
        print("need --url/--token or GRAFANA_URL/GRAFANA_TOKEN", file=sys.stderr)
        return 2
    try:
        res = upload(args.url, args.token, dash)
    except urllib.error.HTTPError as exc:
        print(f"upload failed {exc.code}: {exc.read().decode()[:400]}", file=sys.stderr)
        return 1
    print(f"uploaded: {args.url.rstrip('/')}{res['url']}  (version {res['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
