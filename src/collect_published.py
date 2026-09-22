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
from common import (  # noqa: E402
    BlockedError,
    BudgetExhausted,
    FetchError,
    effective_interval,
    get_json,
    note_throttle_signal,
)
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
    """Fetch a batch, backing off on refusals before splitting.

    A null hitCount is a THROTTLE SIGNAL, not a verdict about the DOIs. Two
    separate lessons are baked in here, both learned the hard way:

    1. It is not "DOI absent". An early version recorded these as `not_found`,
       which pushed the apparent join rate from ~93% down to ~75% -- a stable,
       plausible, completely fictitious result that nearly got written up.
       A refusal is `failed` or retried. Never `not_found`.

    2. It is not "transient, try again immediately" either. A version that
       retried after 0.4s was 403-blocked by Europe PMC within two minutes,
       and the block took over 30 minutes to clear. A null means slow down:
       each one ratchets the sustained pace down via `note_throttle_signal`
       and waits 2, 4, 8, 16, 32s before trying again.
    """
    for attempt in range(NULL_RETRIES):
        try:
            return fetch_batch(dois)
        except SilentBatchFailure:
            # A null hitCount is Europe PMC saying "too fast", not "not there".
            # Ratchet the sustained pace DOWN and wait longer each time.
            # Retrying faster here is what earned a 30-minute 403 block.
            new_interval = note_throttle_signal("www.ebi.ac.uk")
            wait = 2.0 * (2 ** attempt)
            if attempt == 0:
                log(f"  throttled; pace now {new_interval:.2f}s/req")
            time.sleep(wait)

    if len(dois) == 1:
        log(f"  single-DOI query still refused after {NULL_RETRIES} tries: {dois[0]}")
        return [_row(dois[0], "failed")]  # explicitly NOT not_found

    mid = len(dois) // 2
    log(f"  batch of {len(dois)} refused after {NULL_RETRIES} tries; splitting")
    return fetch_batch_safe(dois[:mid]) + fetch_batch_safe(dois[mid:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument(
        "--shard",
        default="",
        help="split the work across machines, e.g. --shard 2/4 for the 2nd of 4. "
             "Each shard writes its own output file, so several people can run "
             "in parallel from different IPs and concatenate afterwards.",
    )
    args = ap.parse_args()

    shard_i = shard_n = 0
    global OUT_CSV
    if args.shard:
        shard_i, shard_n = (int(x) for x in args.shard.split("/"))
        if not 1 <= shard_i <= shard_n:
            ap.error("--shard must be like 2/4 with 1 <= i <= n")
        OUT_CSV = OUT_CSV.replace(".csv", f".shard{shard_i}of{shard_n}.csv")

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
            if not doi or doi in seen:
                continue
            seen.add(doi)
            if shard_n and (len(seen) - 1) % shard_n != (shard_i - 1):
                continue
            if doi not in done:
                wanted.append(doi)

    if args.limit:
        wanted = wanted[: args.limit]

    log(f"=== stage 2: {len(wanted)} DOIs to fetch ({len(done)} already done) ===")
    log(f"=== {(len(wanted) + BATCH - 1) // BATCH} batched requests at {BATCH}/request ===")
    log(f"=== starting pace {effective_interval('www.ebi.ac.uk'):.2f}s/request ===")

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
                log(f"  {processed}/{len(wanted)}  ok={ok} ({ok/max(processed,1):.1%})  "
                    f"pace={effective_interval('www.ebi.ac.uk'):.2f}s  {counts}")

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
