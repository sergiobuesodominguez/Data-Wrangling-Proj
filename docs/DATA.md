# Data: how to get it, what's in it, how it was built

## Getting the data

The raw corpus is deliberately **not** in git (384 MB uncompressed, ~85 MB
gzipped, plus licensing questions — see the README). Three options:

**1. Rebuild it from the cache** — instant, if you have the `data/cache/`
directory from a previous run:

```bash
python3 src/recover_from_cache.py
```

**2. Rebuild it from the API** — no cache needed, ~1–2 hours wall clock
because the pacing is deliberately polite and each run stops at 700 requests:

```bash
# Run this repeatedly until it prints "harvest COMPLETE".
# It is resumable and skips everything already done.
python3 src/collect_biorxiv.py --start 2019-01 --end 2026-08 --sources biorxiv,medrxiv
echo "exit code: $?"   # 0 = every window verified; 1 = windows still outstanding
```

**3. Ask Sergio for the prebuilt `pairs.csv.gz`** (~85 MB). Fastest path, and
identical output to option 2.

---

## Schema: `data/raw/pairs.csv`

One row per unique `(preprint_doi, published_doi)` pair. 200,364 rows.

| Column | Notes |
|---|---|
| `source` | `biorxiv` or `medrxiv` |
| `preprint_doi` | Normalised lowercase, no `https://doi.org/` prefix |
| `published_doi` | Normalised the same way. Join key for stage 2 |
| `published_journal` | Free text as bioRxiv reports it — **not** normalised; 8,074 distinct values, so expect variants of the same journal |
| `preprint_title` | |
| `preprint_authors` | Semicolon-separated, `Surname, I.` form |
| `preprint_category` | bioRxiv subject category, 98 distinct |
| `preprint_date` | Can predate the window by years — windows are keyed on *publication* date |
| `published_date` | The date the harvest window is keyed on |
| `preprint_abstract` | **The "before" text.** Present in 100% of rows, median 1,572 chars |

## Schema: `data/raw/published_abstracts.csv` (stage 2)

One row per unique `published_doi`. Join to `pairs.csv` on `published_doi`.

| Column | Notes |
|---|---|
| `published_doi` | Join key |
| `status` | `ok` · `no_abstract` · `not_found` · `failed` — **always explicit** |
| `source_db` | Europe PMC source (`MED`, `PPR`, `AGR`, …) |
| `pmid` | When available |
| `published_abstract` | **The "after" text.** Only meaningful when `status == ok` |

**Never filter this table by `published_abstract != ""` and move on.** Use
`status`. The reason codes exist so missingness can be characterised — whether
`not_found` correlates with journal, year, or field is itself an analysis, and
silently dropping those rows would bias the sample in exactly the direction the
study is about.

---

## Join rate: measured, and lower than previously claimed

| Measurement | n | `ok` rate |
|---|---|---|
| Original spot check (earlier session) | 30 | ~93% |
| First 30 DOIs in file order | 30 | 73% (22/30) |
| **Random sample, seed 7** | **200** | **75.0%** (150/200) |

**Use 75%.** The 93% figure in the older project pitch does not reproduce and
should not be quoted anywhere. The 200-DOI random sample breaks down as
150 `ok`, 46 `not_found`, 4 `no_abstract`, 0 `failed` — so the shortfall is
Europe PMC not indexing the DOI at all, not abstracts that exist but are empty.

It is stable across time, which rules out "recent papers not yet indexed":

| Year | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| `ok` / n | 9/13 | 18/26 | 32/38 | 23/33 | 13/19 | 16/21 | 26/32 | 13/18 |

**What this means for the study:** ~75% of 200,364 is roughly **150,000 usable
pairs** — still a very large sample, and more than enough. But ~50,000 pairs
will be lost unless recovered, and *those losses are not necessarily random*.
Check whether `not_found` correlates with journal or field before treating the
remainder as a fair sample; that check is itself a reportable methods result.

The obvious recovery is a **Crossref fallback** for
`not_found` DOIs (Crossref has abstracts for many publishers Europe PMC
doesn't index). That would be a new `src/collect_crossref.py` consuming the
`not_found` rows — additive, nothing upstream changes.

---

## Provenance: `data/raw/manifest.json`

184 entries, one per `(source, month)` window, each recording:

```json
"biorxiv:2021-03": {
  "status": "complete",        // complete | empty | partial | failed
  "api_total": 2436,           // what the API said the window contains
  "rows": 2436,                // what we actually banked
  "pages": 25,
  "checked_at": "2026-09-22T12:32:43"
}
```

Final state: **181 complete, 3 empty, 0 outstanding.** The three empty windows
(`medrxiv:2019-02`, `medrxiv:2019-03`, `medrxiv:2019-06`) have `api_total: 0` —
genuinely empty, verified, not blocked. medRxiv launched in mid-2019, so early
gaps there are expected.

This file is the audit trail. `rows` vs `api_total` is the check that makes
"complete" a verified claim rather than a hopeful one.

---

## Collection history, so you don't repeat it

- **First attempt** (earlier session): 38,690 rows, ~50 of 84 months silently
  empty, medRxiv never run. Cause: exhausted retries returned `None`, and the
  caller logged that as an empty month. Discarded entirely.
- **Rebuild, run 1**: 71 windows / 119,239 rows, then `HTTP 403` after ~2,200
  requests at 0.6 s spacing. Blocked for the rest of the run. The process later
  died before writing its CSV — the rows survived only because every page was
  already in the disk cache, which is why `recover_from_cache.py` exists.
- **Rebuild, runs 2–3**: pacing slowed to 1.5 s, a 700-request self-imposed
  budget per run, and 5/10/15-minute cool-offs on 403. One 403 was hit and
  waited out successfully. Finished clean: 184/184 windows.

The lesson worth carrying: **the binding constraint was cumulative volume, and
the dangerous bug was not the block but the inability to tell a block from an
empty month.**
