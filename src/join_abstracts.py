"""Stage 3 -- join the preprint pairs to their published abstracts.

Offline, no network. Produces data/raw/paired_abstracts.csv: every row of
pairs.csv plus the stage-2 columns for its published_doi. This is the
before/after corpus the study actually analyses.

Every pair is kept, including the ones whose published abstract was not
found. The `status` column says why a row lacks its "after" text, and that
missingness is itself something to analyse (see docs/DATA.md). Filter on
`status == "ok"` at analysis time, never here.

Exit code is 1 if any published_doi in pairs.csv has no stage-2 row at all --
that means stage 2 is not actually complete, not that the paper is absent.

Usage:
    python3 src/join_abstracts.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_biorxiv import FIELDS as PAIR_FIELDS, PAIRS_CSV, RAW_DIR, log  # noqa: E402
from collect_published import FIELDS as PUB_FIELDS, OUT_CSV as PUBLISHED_CSV  # noqa: E402

OUT_CSV = os.path.join(RAW_DIR, "paired_abstracts.csv")
FIELDS = PAIR_FIELDS + [f for f in PUB_FIELDS if f != "published_doi"]


def main() -> int:
    csv.field_size_limit(10 ** 9)

    published: dict[str, dict] = {}
    with open(PUBLISHED_CSV, newline="") as fh:
        for row in csv.DictReader(fh):
            published.setdefault(row["published_doi"], row)
    log(f"=== stage 3: {len(published)} published DOIs loaded from {PUBLISHED_CSV} ===")

    counts: Counter = Counter()
    unmatched = 0
    tmp = OUT_CSV + ".tmp"
    with open(PAIRS_CSV, newline="") as src, open(tmp, "w", newline="") as dst:
        w = csv.DictWriter(dst, fieldnames=FIELDS)
        w.writeheader()
        for pair in csv.DictReader(src):
            pub = published.get(pair["published_doi"])
            if pub is None:
                # Not "absent from Europe PMC" -- stage 2 never answered for it.
                unmatched += 1
                pub = {"status": "unmatched", "source_db": "", "pmid": "", "published_abstract": ""}
            row = dict(pair)
            for f in PUB_FIELDS:
                if f != "published_doi":
                    row[f] = pub[f]
            counts[row["status"]] += 1
            w.writerow(row)
    os.replace(tmp, OUT_CSV)

    total = sum(counts.values())
    log(f"=== wrote {total} rows to {OUT_CSV} ===")
    for status, n in counts.most_common():
        log(f"  {status:12s} {n:7d}  {n / total:6.1%}")
    log(f"pairs with both abstracts: {counts['ok']} / {total} = {counts['ok'] / total:.1%}")

    if unmatched:
        log(f"FAILED: {unmatched} pairs have no stage-2 row at all -- rerun collect_published.py")
        return 1
    log("stage 3 COMPLETE: every pair has an explicit stage-2 status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
