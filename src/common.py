"""Polite HTTP layer: disk cache, adaptive per-host throttling, hard-failure semantics.

Design note (this is the fix for the previous harvest's silent data loss):
`get_json` raises FetchError when a request cannot be satisfied. It NEVER
returns None. Callers therefore cannot accidentally treat a failed request as
an empty result -- which is exactly how ~50 of 84 months went missing before.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(WORKSPACE, "data", "cache")

USER_AGENT = (
    "softening-claim-metaresearch/1.0 "
    "(academic claim-strength study; polite crawler, single-threaded)"
)

# Baseline spacing between requests to the same host.
#
# Tuning history, because it matters: a 20-request probe at 0.4s spacing showed
# zero failures, so the first full run used 0.6s -- and was IP-blocked with
# HTTP 403 after ~2,200 requests in 25 minutes. The binding constraint is
# CUMULATIVE VOLUME, not instantaneous rate, which no short probe can detect.
# Hence a slower baseline AND a per-run request budget.
MIN_INTERVAL = 1.5
HOST_INTERVALS = {"www.ebi.ac.uk": 0.34}  # EBI tolerates more than bioRxiv
MAX_RETRIES = 6
BACKOFF_BASE = 4.0
BACKOFF_CAP = 180.0

# A 403 is the host saying "you have had enough", not "try again shortly".
# Quick retries against it are worse than useless: the previous run burned
# 7.5 minutes per window retrying into a wall. Wait in minutes, then give up
# cleanly so the caller can stop and resume in a later run.
BLOCK_WAITS = (300.0, 600.0, 900.0)

# Stop a run on purpose before the host stops it for us. Resumable by design.
REQUEST_BUDGET = int(os.environ.get("REQUEST_BUDGET", "700"))

_last_request_at: dict[str, float] = {}
_requests_made = 0


def requests_made() -> int:
    return _requests_made


class FetchError(RuntimeError):
    """A request could not be satisfied after exhausting all retries."""


class BlockedError(FetchError):
    """The host is refusing us at the IP level (HTTP 403). Stop; resume later."""


class BudgetExhausted(RuntimeError):
    """This run hit its self-imposed request cap. Not an error -- a clean stop."""


def _cache_path(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:24]
    sub = os.path.join(CACHE_DIR, h[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, h + ".json")


def _throttle(host: str, extra: float = 0.0) -> None:
    wait = HOST_INTERVALS.get(host, MIN_INTERVAL) + extra
    last = _last_request_at.get(host)
    if last is not None:
        delta = time.time() - last
        if delta < wait:
            time.sleep(wait - delta)
    _last_request_at[host] = time.time()


def get_json(url: str, *, use_cache: bool = True, label: str = "") -> dict:
    """Fetch and parse JSON, or raise FetchError. Only successes are cached."""
    path = _cache_path(url)
    if use_cache and os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            os.unlink(path)  # corrupt cache entry; refetch

    global _requests_made
    if _requests_made >= REQUEST_BUDGET:
        raise BudgetExhausted(
            f"reached per-run budget of {REQUEST_BUDGET} requests; resume in a fresh run"
        )

    host = urllib.parse.urlsplit(url).netloc
    last_err = ""
    blocks = 0

    for attempt in range(MAX_RETRIES):
        extra = 0.0 if attempt == 0 else min(
            BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1))
        ) + random.uniform(0, 2.0)
        _throttle(host, extra)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            _requests_made += 1
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            payload = json.loads(raw)
            if use_cache:
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(raw)
                os.replace(tmp, path)
            return payload
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    time.sleep(min(BACKOFF_CAP, float(retry_after)))
                except ValueError:
                    pass
            if exc.code in (403, 429):
                if blocks >= len(BLOCK_WAITS):
                    raise BlockedError(
                        f"{label or url}: host still returning {exc.code} after "
                        f"{sum(BLOCK_WAITS)/60:.0f} min of cool-off -- stopping this run"
                    ) from exc
                wait = BLOCK_WAITS[blocks]
                blocks += 1
                print(
                    f"    [blocked {exc.code}] cooling off {wait/60:.0f} min "
                    f"({label or url})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            if exc.code == 404:
                # A genuine "not there" -- distinct from a failure. Cache it so
                # we don't re-ask, but report it as an explicit empty payload.
                if use_cache:
                    with open(path, "w") as fh:
                        json.dump({"__http_404__": True}, fh)
                return {"__http_404__": True}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"

    raise FetchError(f"{label or url}: exhausted {MAX_RETRIES} retries ({last_err})")



def normalise_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip()
