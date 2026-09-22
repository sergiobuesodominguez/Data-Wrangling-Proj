"""Stage 2 -- fetch the PUBLISHED abstract for each pair, via Europe PMC.

Stage 1 gives us the preprint abstract for free. This stage supplies the other
half of the comparison: the abstract as it appeared after peer review.

Europe PMC is used rather than Crossref because it returns the full abstract
text for the large majority of DOIs (measured ~93% on a 30-DOI sample).

Usage:
    python3 src/collect_published.py                 # everything, resumable
    python3 src/collect_published.py --limit 500     # first 500 unfetched
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BlockedError, BudgetExhausted, FetchError, get_json  # noqa: E402
from collect_biorxiv import PAIRS_CSV, RAW_DIR, log  # noqa: E402

OUT_CSV = os.path.join(RAW_DIR, "published_abstracts.csv")
FIELDS = ["published_doi", "status", "source_db", "pmid", "published_abstract"]

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def fetch_published_abstract(doi: str) -> dict:
    """Return a row dict. `status` is always explicit -- never a silent blank."""
    query = urllib.parse.quote(f'DOI:"{doi}"')
    url = f"{ENDPOINT}?query={query}&resultType=core&format=json&pageSize=1"
    payload = get_json(url, label=f"epmc {doi}")

    results = (payload.get("resultList") or {}).get("result") or []
    if not results:
        return {"published_doi": doi, "status": "not_found", "source_db": "",
                "pmid": "", "published_abstract": ""}

    rec = results[0]
    abstract = (rec.get("abstractText") or "").strip()
    return {
        "published_doi": doi,
        "status": "ok" if len(abstract) > 50 else "no_abstract",
        "source_db": rec.get("source") or "",
        "pmid": rec.get("pmid") or "",
        "published_abstract": abstract,
    }


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

    new_file = not os.path.exists(OUT_CSV)
    counts = {"ok": 0, "no_abstract": 0, "not_found": 0, "failed": 0}

    with open(OUT_CSV, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        for i, doi in enumerate(wanted, 1):
            try:
                row = fetch_published_abstract(doi)
            except (BlockedError, BudgetExhausted) as exc:
                log(f"STOPPING at {i}/{len(wanted)}: {exc}")
                break
            except FetchError as exc:
                log(f"failed {doi}: {exc}")
                row = {"published_doi": doi, "status": "failed", "source_db": "",
                       "pmid": "", "published_abstract": ""}
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            w.writerow(row)
            if i % 100 == 0:
                fh.flush()
                log(f"  {i}/{len(wanted)}  {counts}")

    log(f"=== stage 2 done for this run: {counts} ===")
    total = sum(counts.values())
    if total:
        log(f"join rate this run: {counts['ok'] / total:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
