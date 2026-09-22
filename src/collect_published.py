"""Stage 2 -- fetch the PUBLISHED abstract for each pair, via Europe PMC.

Stage 1 gives us the preprint ("before") abstract for free. This stage supplies
the other half of the comparison: the abstract as it appeared after peer review.

WHY BATCHED: one DOI per request would take ~83 hours for 200k DOIs. Europe PMC
accepts boolean OR queries, so we ask for 20 DOIs at a time -- ~10,000 requests
instead of 200,364.

WHY EXACTLY 20: measured. 20 clauses returns results; 25 returns
`hitCount: null` with an empty result list and HTTP 200 -- a SILENT failure, no
error raised. That is the same shape of bug that silently destroyed the first
stage-1 harvest, so `fetch_batch` treats a null hitCount as a hard error and
the caller splits the batch rather than recording 20 false `not_found`s.

Usage:
    python3 src/collect_published.py                 # everything, resumable
    python3 src/collect_published.py --limit 5000    # first 5000 unfetched
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BlockedError, BudgetExhausted, FetchError, get_json  # noqa: E402
from collect_biorxiv import PAIRS_CSV, RAW_DIR, log  # noqa: E402

OUT_CSV = os.path.join(RAW_DIR, "published_abstracts.csv")
FIELDS = ["published_doi", "status", "source_db", "pmid", "published_abstract"]

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
BATCH = 20  # measured ceiling -- do not raise without re-testing


class SilentBatchFailure(FetchError):
    """Europe PMC answered 200 but with a null hitCount -- the query was refused."""


def _row(doi: str, status: str, rec: dict | None = None) -> dict:
    rec = rec or {}
    return {
        "published_doi": doi,
        "status": status,
        "source_db": rec.get("source") or "",
        "pmid": rec.get("pmid") or "",
        "published_abstract": (rec.get("abstractText") or "").strip(),
    }


def fetch_batch(dois: list[str]) -> list[dict]:
    """Fetch up to BATCH DOIs in one query. Returns one row per input DOI."""
    query = " OR ".join(f'DOI:"{d}"' for d in dois)
    url = (
        f"{ENDPOINT}?query={urllib.parse.quote(query)}"
        f"&resultType=core&format=json&pageSize=100"
    )
    payload = get_json(url, label=f"epmc batch of {len(dois)}")

    # The silent-failure guard. An empty result set is legitimate; a null
    # hitCount alongside it means the query itself was rejected.
    if payload.get("hitCount") is None:
        raise SilentBatchFailure(
            f"null hitCount for a batch of {len(dois)} -- query refused, not empty"
        )

    results = (payload.get("resultList") or {}).get("result") or []
    by_doi: dict[str, dict] = {}
    for rec in results:
        doi = (rec.get("doi") or "").strip().lower()
        if doi:
            by_doi.setdefault(doi, rec)

    rows = []
    for doi in dois:
        rec = by_doi.get(doi)
        if rec is None:
            rows.append(_row(doi, "not_found"))
        elif len((rec.get("abstractText") or "").strip()) > 50:
            rows.append(_row(doi, "ok", rec))
        else:
            rows.append(_row(doi, "no_abstract", rec))
    return rows


NULL_RETRIES = 5


def fetch_batch_safe(dois: list[str]) -> list[dict]:
    """Fetch a batch, retrying transient refusals before splitting.

    A null hitCount is TRANSIENT, not a verdict about the DOIs. Measured: the
    same single DOI can return hitCount=null on one call and hitCount=1 on the
    next. An earlier version of this file recorded those as `not_found`, which
    depressed the apparent join rate from ~94% to ~75% and very nearly became
    a published finding about Europe PMC coverage. It was our bug, not their
    coverage. Retry first; only then split; never record a refusal as
    `not_found`.
    """
    for attempt in range(NULL_RETRIES):
        try:
            return fetch_batch(dois)
        except SilentBatchFailure:
            time.sleep(0.4 * (attempt + 1))

    if len(dois) == 1:
        log(f"  single-DOI query still refused after {NULL_RETRIES} tries: {dois[0]}")
        return [_row(dois[0], "failed")]  # explicitly NOT not_found

    mid = len(dois) // 2
    log(f"  batch of {len(dois)} refused after {NULL_RETRIES} tries; splitting")
    return fetch_batch_safe(dois[:mid]) + fetch_batch_safe(dois[mid:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = ap.parse_args()

    csv.field_size_limit(10 ** 9)

    done: set[str] = set()
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV, newline="") as fh:
            done = {r["published_doi"] for r in csv.DictReader(fh)}

    wanted: list[str] = []
    seen: set[str] = set()
    with open(PAIRS_CSV, newline="") as fh:
        for row in csv.DictReader(fh):
            doi = row["published_doi"]
            if doi and doi not in seen and doi not in done:
                seen.add(doi)
                wanted.append(doi)

    if args.limit:
        wanted = wanted[: args.limit]

    log(f"=== stage 2: {len(wanted)} DOIs to fetch ({len(done)} already done) ===")
    log(f"=== {(len(wanted) + BATCH - 1) // BATCH} batched requests at {BATCH}/request ===")

    new_file = not os.path.exists(OUT_CSV)
    counts: dict[str, int] = {}
    processed = 0

    with open(OUT_CSV, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            try:
                rows = fetch_batch_safe(chunk)
            except (BlockedError, BudgetExhausted) as exc:
                log(f"STOPPING after {processed}/{len(wanted)}: {exc}")
                break
            except FetchError as exc:
                log(f"batch failed, recording as failed: {exc}")
                rows = [_row(d, "failed") for d in chunk]

            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
                w.writerow(row)
            processed += len(rows)

            if processed % 1000 < BATCH:
                fh.flush()
                ok = counts.get("ok", 0)
                log(f"  {processed}/{len(wanted)}  ok={ok} ({ok/max(processed,1):.1%})  {counts}")

    log(f"=== stage 2 run finished: {processed} DOIs, {counts} ===")
    total = sum(counts.values())
    if total:
        log(f"join rate this run: {counts.get('ok', 0) / total:.1%}")
    remaining = len(wanted) - processed
    if remaining:
        log(f"{remaining} DOIs still outstanding -- rerun to continue")
        return 1
    log("stage 2 COMPLETE for all requested DOIs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
