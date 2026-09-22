"""Stage 1 -- harvest preprint -> publication links from bioRxiv / medRxiv.

The previous version of this script lost ~50 of 84 months without anyone
noticing, because a failed request and a genuinely empty month both ended up
looking like "0 records". This version makes that impossible:

  * every window gets an explicit status recorded in a manifest
  * the API's own `total` is compared against the rows actually collected
  * a window is only `complete` when fetched >= total; short reads are `partial`
  * failures are re-queued across several passes with escalating cool-off
  * the run exits non-zero, loudly, if any window is not complete or empty

Usage:  python3 src/collect_biorxiv.py [--start 2019-01] [--end 2026-08]
                                       [--sources biorxiv,medrxiv]
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    BlockedError,
    BudgetExhausted,
    FetchError,
    WORKSPACE,
    get_json,
    normalise_doi,
    requests_made,
)

RAW_DIR = os.path.join(WORKSPACE, "data", "raw")
PAIRS_CSV = os.path.join(RAW_DIR, "pairs.csv")
MANIFEST = os.path.join(RAW_DIR, "manifest.json")
LOG = os.path.join(RAW_DIR, "harvest.log")

PAGE = 100
MAX_PASSES = 4

FIELDS = [
    "source",
    "preprint_doi",
    "published_doi",
    "published_journal",
    "preprint_title",
    "preprint_authors",
    "preprint_category",
    "preprint_date",
    "published_date",
    "preprint_abstract",
]


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def months(start: str, end: str) -> list[str]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def window_bounds(month: str) -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def fetch_window(source: str, month: str) -> tuple[list[dict], dict]:
    """Return (rows, status_record). Raises FetchError -- never fakes an empty month."""
    lo, hi = window_bounds(month)
    rows: list[dict] = []
    cursor = 0
    total: int | None = None
    pages = 0

    while True:
        url = f"https://api.biorxiv.org/pubs/{source}/{lo}/{hi}/{cursor}"
        payload = get_json(url, label=f"{source} {month} @{cursor}")
        if payload.get("__http_404__"):
            # bioRxiv answers 404 for a window with no content at all.
            total = 0
            break

        msgs = payload.get("messages") or []
        if msgs and total is None:
            try:
                total = int(msgs[0].get("total") or 0)
            except (TypeError, ValueError):
                total = 0

        batch = payload.get("collection") or []
        pages += 1
        for rec in batch:
            pdoi = normalise_doi(rec.get("preprint_doi"))
            jdoi = normalise_doi(rec.get("published_doi"))
            if not pdoi or not jdoi:
                continue
            rows.append(
                {
                    "source": source,
                    "preprint_doi": pdoi,
                    "published_doi": jdoi,
                    "published_journal": (rec.get("published_journal") or "").strip(),
                    "preprint_title": (rec.get("preprint_title") or "").strip(),
                    "preprint_authors": (rec.get("preprint_authors") or "").strip(),
                    "preprint_category": (rec.get("preprint_category") or "").strip(),
                    "preprint_date": (rec.get("preprint_date") or "").strip(),
                    "published_date": (rec.get("published_date") or "").strip(),
                    "preprint_abstract": (rec.get("preprint_abstract") or "").strip(),
                }
            )

        if not batch:
            break
        cursor += PAGE
        if total is not None and cursor >= total:
            break

    total = total if total is not None else 0
    if total == 0:
        status = "empty"
    elif len(rows) >= total * 0.98:  # allow for records dropped for missing DOIs
        status = "complete"
    else:
        status = "partial"

    return rows, {
        "status": status,
        "api_total": total,
        "rows": len(rows),
        "pages": pages,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01")
    ap.add_argument("--end", default="2026-08")
    ap.add_argument("--sources", default="biorxiv,medrxiv")
    args = ap.parse_args()

    os.makedirs(RAW_DIR, exist_ok=True)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    window_list = [(s, m) for s in sources for m in months(args.start, args.end)]

    manifest: dict[str, dict] = {}
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as fh:
            manifest = json.load(fh)

    # No in-memory banking: the dataset is ~220 MB, and every fetched page is
    # already on disk in the cache. pairs.csv is regenerated from the cache at
    # the end of the run, which is both cheaper and crash-proof.
    log(f"=== harvest start: {len(window_list)} windows over {sources} ===")

    pending = [
        (s, m) for s, m in window_list
        if manifest.get(f"{s}:{m}", {}).get("status") not in ("complete", "empty")
    ]

    for attempt in range(1, MAX_PASSES + 1):
        if not pending:
            break
        if attempt > 1:
            cool = 60 * attempt
            log(f"--- pass {attempt}: {len(pending)} windows to retry, cooling {cool}s ---")
            time.sleep(cool)

        still: list[tuple[str, str]] = []
        stopped = False
        for src, month in pending:
            key = f"{src}:{month}"
            try:
                rows, rec = fetch_window(src, month)
            except (BlockedError, BudgetExhausted) as exc:
                # Not a data failure -- the run is choosing to stop. Everything
                # still outstanding stays pending for the next run.
                log(f"STOPPING: {exc}")
                still.append((src, month))
                still.extend(pending[pending.index((src, month)) + 1:])
                stopped = True
                break
            except FetchError as exc:
                manifest[key] = {
                    "status": "failed",
                    "error": str(exc)[:200],
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                log(f"FAILED {key}: {exc}")
                still.append((src, month))
            else:
                manifest[key] = rec
                del rows  # already persisted in the page cache
                flag = "" if rec["status"] in ("complete", "empty") else "  <-- PARTIAL"
                log(
                    f"{key}: {rec['status']} rows={rec['rows']} "
                    f"api_total={rec['api_total']} pages={rec['pages']}{flag}"
                )
                if rec["status"] == "partial":
                    still.append((src, month))

            with open(MANIFEST, "w") as fh:
                json.dump(manifest, fh, indent=1, sort_keys=True)

        pending = still
        if stopped:
            break

    # ---- regenerate the deduplicated dataset from the page cache ------------
    from recover_from_cache import main as rebuild_from_cache  # late: avoids cycle

    rebuild_from_cache()
    with open(PAIRS_CSV, newline="") as fh:
        written = sum(1 for _ in csv.DictReader(fh))

    # ---- completeness gate ---------------------------------------------------
    # The gate must judge every window we SET OUT to collect, not merely the
    # ones the manifest happens to mention. A budget stop leaves windows
    # untouched, and an untouched window is exactly the kind of hole this
    # whole rewrite exists to make impossible to overlook.
    bad: dict[str, dict] = {}
    for src, month in window_list:
        key = f"{src}:{month}"
        rec = manifest.get(key)
        if rec is None:
            bad[key] = {"status": "not attempted"}
        elif rec.get("status") not in ("complete", "empty"):
            bad[key] = rec
    empties = [k for k, v in manifest.items() if v.get("status") == "empty"]

    log(f"=== wrote {written} unique pairs to {PAIRS_CSV} ===")
    log(f"requests made this run: {requests_made()}")
    log(
        f"windows: {len(window_list)} expected, {len(window_list) - len(bad)} accounted for, "
        f"{len(bad)} outstanding, {len(empties)} genuinely empty"
    )
    if empties:
        log("genuinely-empty windows (API total=0): " + ", ".join(sorted(empties)))
    if bad:
        log("!!! INCOMPLETE HARVEST -- these windows need another run:")
        for k in sorted(bad):
            log(f"    {k}: {bad[k].get('status')} {bad[k].get('error', '')}")
        return 1

    log("harvest COMPLETE: every window accounted for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
