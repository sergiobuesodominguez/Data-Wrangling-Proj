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

**3. Download the prebuilt `pairs.csv.gz`** (138 MB compressed, 384 MB
uncompressed). Fastest path, identical to what option 2 rebuilds:

```bash
curl -L -o pairs.csv.gz \
  "https://pub.hyperagent.com/api/published/pbf01M34W7S9R_1468YPPSBS7B33WS/pairs.csv.gz"
gunzip -c pairs.csv.gz > data/raw/pairs.csv
wc -l data/raw/pairs.csv      # abstracts contain newlines, so this exceeds 200,364
python3 -c "import csv,sys; csv.field_size_limit(10**9); \
print(sum(1 for _ in csv.DictReader(open('data/raw/pairs.csv'))), 'rows')"
# expected: 200364 rows
```

If that link has expired by the time you read this, ask Sergio — or just run
option 2, which reproduces it exactly.

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

## Join rate: 92.8%

| Measurement | n | `ok` rate | Verdict |
|---|---|---|---|
| Original spot check (first session) | 30 | ~93% | ✅ was right |
| Single-DOI pass, buggy | 30 | 73% | ❌ artefact |
| Random sample, buggy code | 200 | 75.0% | ❌ artefact |
| **Random sample, fixed code, seed 11** | **600** | **92.8%** | ✅ **use this** |

Final: **557 `ok`, 40 `not_found`, 3 `no_abstract`, 0 `failed`** out of 600.
About **186,000 of 200,364 pairs** should end up with both abstracts.

### How the 75% artefact happened — don't recreate it

Europe PMC intermittently returns `HTTP 200` with `hitCount: null` and an
empty `resultList`. That is the API *refusing the query*, not reporting that
the DOI is absent. The same DOI returns `hitCount: 1` moments later.

The original single-DOI collector saw "no results" and wrote `not_found`.
Because the refusals are roughly random, the corruption was uniform across
years — producing a 75% rate that looked stable, plausible, and publishable.
Nothing about the output looked broken.

It was caught only because the batched rewrite scored 94.5% on the same data,
and the contradiction had to be explained rather than averaged away.

**The rules that came out of it:**

1. A refused query is **never** `not_found`. It is `failed`, or it is retried.
2. `fetch_batch_safe` retries a null hitCount 5 times before splitting.
3. Batch size is **20**. At 25, Europe PMC refuses *every* query the same
   silent way — which would have written 200,364 false `not_found` rows.
4. If the join rate moves, suspect the collector before believing the finding.

### Recovering the genuine residual

The real ~7% `not_found` is a coverage limit. A **Crossref fallback**
(`src/collect_crossref.py`, stage 2b) consuming the `not_found` rows would
recover much of it — additive, nothing upstream changes.

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
