"""Run identity, timestamps and live annotations.

One definition site for three things every tool here needs to agree on.

IDENTITY. Two levels, because they answer different questions:

    experiment_id   the campaign or configuration -- one subject at one setting.
                    Results from different experiment_ids are NEVER pooled, and
                    carrying the id in every row is what makes that a filter
                    rather than a filing convention.
    run_id          one load sweep inside an experiment. Unique, sortable, and
                    carries its own seed so the shuffled order is reconstructible
                    from the name alone.

TIME. Every timestamp is stored twice: the epoch float is AUTHORITATIVE and is the
only thing arithmetic is ever done on; the ISO-8601 string beside it is for humans
and is always UTC with an explicit offset. A naive local-time string is ambiguous
across a daylight-saving boundary, and a measurement campaign that straddles one
would silently acquire a one-hour error in anything derived from the string.
"""

import datetime
import json
import re
import urllib.error
import urllib.request

ISO_NOTE = "UTC, explicit offset; epoch is authoritative, this string is for humans"


def iso(ts):
    """Epoch seconds -> ISO-8601 in UTC with an explicit offset."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def stamp(ts):
    """A timestamp as both forms, for embedding in a JSON record."""
    return {"epoch": ts, "iso": iso(ts)}


def slug(text):
    """Make a string safe for an id and a filename, without silently emptying it."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-")
    if not s:
        raise ValueError(f"id {text!r} contains no usable characters")
    return s


def make_run_id(experiment_id, seed, when=None):
    """e.g. 'cdn-baseline.20261007T140000Z.s42'

    Sorts chronologically inside an experiment, and states the seed so the level
    order can be regenerated from the id without opening the session file.
    """
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return f"{slug(experiment_id)}.{when.strftime('%Y%m%dT%H%M%SZ')}.s{int(seed)}"


class Annotator:
    """Posts level boundaries to Grafana so a sweep can be watched while it runs.

    Deliberately best-effort: a failed annotation prints a warning and the sweep
    continues. Supervision must never be able to stop a measurement, and nothing
    read off a dashboard is ever a result -- the dashboard may use rate(), which
    is exactly what the analysis refuses to use.
    """

    def __init__(self, url=None, token=None, dashboard_uid=None):
        self.url = url.rstrip("/") if url else None
        self.token = token
        self.dashboard_uid = dashboard_uid
        self.failures = 0
        self.posted = 0

    @property
    def enabled(self):
        return bool(self.url and self.token)

    def annotate(self, text, tags, start, end=None):
        if not self.enabled:
            return False
        body = {
            "time": int(start * 1000),
            "text": text,
            "tags": list(tags),
        }
        if end is not None:
            body["timeEnd"] = int(end * 1000)
        if self.dashboard_uid:
            body["dashboardUID"] = self.dashboard_uid
        req = urllib.request.Request(
            f"{self.url}/api/annotations",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                self.posted += 1
                return True
        except (urllib.error.URLError, OSError) as exc:
            self.failures += 1
            if self.failures <= 3:
                print(f"    [annotation failed, continuing: {exc}]")
            return False
