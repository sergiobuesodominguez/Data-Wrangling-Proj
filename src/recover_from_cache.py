"""Rebuild pairs.csv purely from the disk cache -- no network calls at all.

The harvest process died before its final write, so ~119k already-fetched rows
existed only as cached JSON pages. This script walks the cache directly, so the
data is banked before anything touches the API again.
"""
from __future__ import annotations

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CACHE_DIR, normalise_doi  # noqa: E402
from collect_biorxiv import FIELDS, PAIRS_CSV, RAW_DIR  # noqa: E402


def main() -> int:
    os.makedirs(RAW_DIR, exist_ok=True)
    seen: set[tuple[str, str]] = set()
    pages = kept = skipped = 0

    tmp = PAIRS_CSV + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()

        for root, _dirs, files in os.walk(CACHE_DIR):
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path) as cf:
                        payload = json.load(cf)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                if not isinstance(payload, dict) or payload.get("__http_404__"):
                    continue
                batch = payload.get("collection") or []
                if not batch:
                    continue
                pages += 1

                for rec in batch:
                    pdoi = normalise_doi(rec.get("preprint_doi"))
                    jdoi = normalise_doi(rec.get("published_doi"))
                    if not pdoi or not jdoi:
                        continue
                    ident = (pdoi, jdoi)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    platform = (rec.get("preprint_platform") or "").strip().lower()
                    w.writerow(
                        {
                            "source": "medrxiv" if "med" in platform else "biorxiv",
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
                    kept += 1

    os.replace(tmp, PAIRS_CSV)
    print(f"cache pages read : {pages}")
    print(f"unreadable pages : {skipped}")
    print(f"unique pairs     : {kept}")
    print(f"written to       : {PAIRS_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
