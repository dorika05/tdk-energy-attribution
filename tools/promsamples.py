"""Raw per-sample reads from Prometheus.

One definition site for "how a sample is obtained". Every tool here imports this
rather than building its own query, because the distinction it encodes is the one
the whole validity argument rests on:

    /api/v1/query with a RANGE-VECTOR SELECTOR  ->  real samples, real timestamps
    /api/v1/query_range with step=              ->  values re-stamped onto YOUR grid

Demonstrated on this cluster 2026-10-07, same series, same 60 s:

    selector  [1791373597.998, '224429.583722']   <- when the exporter was scraped
    range     [1791373600,     '224429.583722']   <- same value, displaced 2.002 s

Both report the value. Only the first reports when it was taken, and an edge-offset
check, a counter difference over a true elapsed time, and any after-the-fact
re-cutting of interval boundaries all need that timestamp. query_range is therefore
never used for measurement here. It is fine for a dashboard.
"""

import json
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:19090"

# Prometheus refuses a query touching more than ~50M samples, and a single HTTP
# response holding hours of a wide family is slow to parse before it is large.
# 30 min per request at a 10 s cadence is ~180 points/series, which keeps both
# comfortable. Lowered only if a family turns out to be unusually wide.
CHUNK_SECONDS = 1800


def _get(url, path, params, timeout=120):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}{path}?{q}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus: {payload.get('error', payload)}")
    return payload["data"]


def instant(expr, url=DEFAULT_URL, timeout=120):
    """Evaluate expr once. For declarations and probes, never for measurement."""
    return _get(url, "/api/v1/query", {"query": expr}, timeout)


def label_values(label, url=DEFAULT_URL, timeout=120):
    return _get(url, f"/api/v1/label/{label}/values", {}, timeout)


def samples(selector, start, end, url=DEFAULT_URL, chunk=CHUNK_SECONDS, timeout=180):
    """Every raw sample of `selector` in [start, end], with its own timestamp.

    `selector` is a matcher without a range, e.g. 'hardware_cpu_seconds_total{node="node2"}'.
    The range is appended here so no caller can accidentally pass a step instead.

    Returns {series_key: {"labels": {...}, "points": [(ts_float, value_str), ...]}}
    with points sorted and de-duplicated across chunk boundaries.
    """
    out = {}
    t = float(start)
    end = float(end)
    while t < end:
        hi = min(t + chunk, end)
        span = hi - t
        # Stop on a residual too small to ask for. A window whose length is a hair
        # over a multiple of `chunk` leaves a sub-second tail here, and Prometheus
        # rejects a zero-length range selector with HTTP 400 -- which surfaced as
        # "no reference family returned samples" rather than as an error.
        if span < 1.0:
            break
        # The range is rounded UP so a fractional span can never truncate to a
        # shorter window and drop the samples near `hi`. Rounding up makes the
        # lookback reach slightly before `t`, which can return a sample from before
        # the requested window; the filter below removes those, and the dedup
        # removes the overlap with the previous chunk.
        #
        # A range-vector selector is evaluated AT its right edge and looks back
        # over the range, so asking at `hi` for `[span]` yields (hi-span, hi].
        rng = int(span) + (1 if span % 1 else 0)
        data = _get(url, "/api/v1/query",
                    {"query": f"{selector}[{rng}s]", "time": repr(hi)}, timeout)
        for series in data.get("result", []):
            labels = series["metric"]
            key = json.dumps(labels, sort_keys=True)
            slot = out.setdefault(key, {"labels": labels, "points": []})
            slot["points"].extend((float(ts), val) for ts, val in series.get("values", []))
        t = hi
    for slot in out.values():
        seen, uniq = set(), []
        for ts, val in sorted(slot["points"]):
            # Half-open on the left so a sample exactly at `start` is excluded,
            # matching the (start, end] semantics of the selector itself.
            if ts not in seen and start < ts <= end:
                seen.add(ts)
                uniq.append((ts, val))
        slot["points"] = uniq
    return out
