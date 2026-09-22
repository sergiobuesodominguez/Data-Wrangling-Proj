# Softening the Claim

**Does peer review soften scientific claims?**

Every preprint on bioRxiv/medRxiv that later appears in a journal gives us the
same paper's abstract twice: once as the authors wrote it, and once after peer
review. That is a natural before/after pair, at scale, for a question everyone
in research has an opinion about and nobody has actually measured.

This repository is the **data collection base** for that study. Stage 1 is
finished and verified; stage 2 runs and is ready to scale up.

---

## What exists right now

| | |
|---|---|
| **Pairs collected** | **200,364** unique preprint → publication pairs |
| **Sources** | bioRxiv 160,488 · medRxiv 39,876 |
| **Publication years** | 2019 – 2026 (Aug), complete |
| **Journals** | 8,074 |
| **Subject categories** | 98 |
| **Preprint abstracts** | 200,364 / 200,364 — every single row has one |
| **Median abstract length** | 1,572 characters |
| **Collection windows** | 184 expected, 184 accounted for, **0 outstanding** |

Top journals: eLife (8,869), Nature Communications (8,663), PLOS ONE (6,978),
Scientific Reports (5,542), PNAS (3,779).
Top categories: neuroscience (30,245), microbiology (15,075), bioinformatics
(12,914), cell biology (9,312), biophysics (8,651).

**The preprint ("before") abstract ships with stage 1 at no extra cost** — the
bioRxiv `/pubs/` endpoint includes it. Only the published ("after") abstract
needs a second lookup, which is stage 2.

---

## Quick start

No dependencies beyond the Python 3 standard library.

```bash
git clone https://github.com/sergiobuesodominguez/Data-Wrangling-Proj.git
cd Data-Wrangling-Proj

# Look at the data without downloading anything (300 real rows, committed):
head -c 2000 data/samples/pairs_sample_300.csv

# Rebuild the full stage-1 dataset from scratch (~1-2 h, see the warning below):
python3 src/collect_biorxiv.py --start 2019-01 --end 2026-08 --sources biorxiv,medrxiv

# Fetch published abstracts for the first 500 pairs:
python3 src/collect_published.py --limit 500
```

`data/raw/pairs.csv` is **not in this repo** — it is 384 MB. See
[docs/DATA.md](docs/DATA.md) for how to get the prebuilt copy or rebuild it.

---

## Repository layout

```
src/common.py               HTTP layer: cache, throttling, backoff, hard-failure semantics
src/collect_biorxiv.py      Stage 1 -- preprint -> publication links + preprint abstracts
src/collect_published.py    Stage 2 -- published abstracts via Europe PMC
src/recover_from_cache.py   Rebuild pairs.csv from the page cache, no network
data/raw/manifest.json      Per-window provenance: status, API total, rows, pages, timestamp
data/samples/               300 real rows, committed so the schema is inspectable
docs/DATA.md                How to obtain/rebuild the data, and the schema
```

---

## Read this before you point it at the API

This pipeline was rebuilt after a first attempt **silently lost ~50 of 84
months** of data. Both failure modes are worth knowing, because they will bite
anyone who changes the pacing:

**1. bioRxiv blocks on cumulative volume, not on rate.** A 20-request probe at
0.4 s spacing showed zero failures. A full run at 0.6 s spacing was hit with
`HTTP 403` after roughly 2,200 requests and stayed blocked for hours. No short
probe can detect this. Hence `MIN_INTERVAL = 1.5` and `REQUEST_BUDGET = 700`
in `src/common.py`: **a run stops itself on purpose before the host stops it.**
Runs are fully resumable — just run the same command again.

**2. A failed request must never look like an empty result.** The original bug
was that `get_json` returned `None` on exhausted retries and the caller logged
that as "0 records for this month". Fifty empty months looked exactly like
fifty quiet months. The current design makes that impossible:

- `get_json` **raises** `FetchError` / `BlockedError`; it never returns `None`
- every window's outcome is recorded in `manifest.json` with an explicit status
- rows collected are reconciled against **the API's own `total`** for that window
- a short read is `partial`, not success, and gets re-queued
- the completeness gate checks **every window that was asked for**, including
  ones never attempted, and exits non-zero listing each one

If `collect_biorxiv.py` exits 0 and says `harvest COMPLETE: every window
accounted for`, that statement has been checked against the API's own counts.

A `403` triggers cool-offs of 5, 10, then 15 minutes before the run gives up
cleanly and leaves the rest for next time. Don't replace that with fast retries.

---

## Where stage 2 stands

**Join rate: 92.8%**, measured on a 600-DOI random sample (557 `ok`, 40
`not_found`, 3 `no_abstract`). So roughly **186,000 of the 200,364 pairs will
have both halves of the comparison.**

Stage 2 is batched — 20 DOIs per query, ~10,000 requests instead of 200,364,
which is the difference between ~3 hours and ~83 hours.

### A cautionary tale worth reading before you touch this file

The join rate was briefly "measured" at 75% and that number was wrong. Europe
PMC intermittently answers `HTTP 200` with `hitCount: null` and an empty
result list — a *transient refusal of the query*, not a statement that the DOI
is absent. The first version of this collector recorded each of those as
`not_found`.

The result was a plausible, stable, entirely fictitious finding: 75% across a
200-DOI random sample, consistent across every year, which is exactly what a
real coverage limit would look like. It was our bug. Retrying the same DOIs
returns them fine.

`fetch_batch_safe` now retries a null hitCount five times before splitting the
batch, and a query that is still refused is recorded as **`failed`, never
`not_found`**. Keep that distinction: one means "we asked and it isn't there",
the other means "we never got a real answer". Collapsing them is how you
manufacture a finding.

`not_found` at ~7% is the genuine residual. A Crossref fallback (stage 2b)
could recover much of it if you want those rows.

### Run stage 2 locally — and split it between you

**Do not run this from a shared or cloud IP.** Europe PMC throttles by address.
The machine this dataset was collected on got 403-blocked, and afterwards
crawled at ~1 DOI/second even with polite pacing — roughly 50 hours for the
remaining 197k. From a normal home or university connection the same code runs
far faster, because the address has no penalty against it.

Solo:

```bash
python3 src/collect_published.py            # resumable; rerun until it says COMPLETE
```

Split across four people (each on their own connection):

```bash
python3 src/collect_published.py --shard 1/4   # person 1
python3 src/collect_published.py --shard 2/4   # person 2
python3 src/collect_published.py --shard 3/4   # person 3
python3 src/collect_published.py --shard 4/4   # person 4
```

Each shard writes `published_abstracts.shardNofM.csv` — disjoint DOI sets,
verified to partition exactly, no overlap and nothing dropped. Merge with:

```bash
python3 - <<'PY'
import csv, glob
csv.field_size_limit(10**9)
rows, seen = [], set()
for f in sorted(glob.glob('data/raw/published_abstracts*.csv')):
    for r in csv.DictReader(open(f, newline='')):
        if r['published_doi'] not in seen:
            seen.add(r['published_doi']); rows.append(r)
with open('data/raw/published_abstracts.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print(len(rows), 'unique DOIs merged')
PY
```

Then join to `pairs.csv` on `published_doi` — that join is the actual
before/after dataset the study needs.

### Step by step for a collaborator

**1. Get the repo.** Public, so no login prompt.

```bash
git clone https://github.com/sergiobuesodominguez/Data-Wrangling-Proj.git
cd Data-Wrangling-Proj
```

**2. Get the stage-1 dataset** (138 MB compressed, 384 MB on disk).

```bash
mkdir -p data/raw
curl -L -o data/raw/pairs.csv.gz \
  "https://pub.hyperagent.com/api/published/pbf01M34W7S9R_1468YPPSBS7B33WS/pairs.csv.gz"
gunzip -c data/raw/pairs.csv.gz > data/raw/pairs.csv
```

**3. Seed the ~2,900 abstracts already collected.** Skips work you would
otherwise redo. The script treats a DOI found in *any*
`data/raw/published_abstracts*.csv` as done, so this seed is honoured by solo
and `--shard` runs alike.

```bash
curl -L -o data/raw/pa.csv.gz \
  "https://pub.hyperagent.com/api/published/pbf01M35BF29Q_CEF4GPFF0CKTHKZK/published_abstracts_partial.csv.gz"
gunzip -c data/raw/pa.csv.gz > data/raw/published_abstracts.csv
```

**4. Verify before the long run.**

```bash
python3 -c "
import csv; csv.field_size_limit(10**9)
p=list(csv.DictReader(open('data/raw/pairs.csv')))
a=list(csv.DictReader(open('data/raw/published_abstracts.csv')))
print(len(p),'pairs |',len(a),'abstracts already done')"
```

Expected: `200364 pairs | 2881 abstracts already done`. If you see that,
you're good.

**5. Run it** — solo, or one shard each as shown above. Watch the `pace=`
figure in the progress lines. If it climbs past ~3 s/req, that address is
being throttled: pause an hour rather than pushing through. Rerun the same
command any time; it skips everything already collected.

---

## Suggested workstream split

Each has a real first deliverable and a clean interface, so nobody blocks
anyone else:

| | Workstream | Owns | Interface |
|---|---|---|---|
| **A** | Collection & provenance | The APIs, caching, join-failure reason codes, the datasheet, licensing | Produces `pairs.csv` + `published_abstracts.csv`; downstream only reads them |
| **B** | Annotation & agreement | Claim-strength codebook, ~250-pair human gold set, AI annotation pass, Cohen's κ / Krippendorff's α | Consumes pairs, emits labels keyed by DOI |
| **C** | Text processing & features | Sentence segmentation, preprint↔published alignment, hedge/booster lexicon scoring, missingness | Consumes pairs, emits features keyed by DOI |
| **D** | Analysis & visualization | Paired tests, regression, time series, dimensionality reduction, figures, interactive explorer | Consumes features + labels |

B and C both measure claim strength — one by human/AI judgement, one by
transparent lexicon rules. That redundancy is deliberate: if they agree the
finding is robust; if they disagree, **the disagreement is itself a result**
about the limits of AI annotation. Neither failing can sink the project.

---

## Known limitations

- **Windows are keyed on publication date**, so `preprint_date` legitimately
  reaches back years before the window. Not a bug; state it in the datasheet.
- **Correlational, not causal.** Slow or selective journals attract different
  papers, and desk rejections never enter the sample. Keep the language
  correlational and name the confounders.
- **Survivorship by construction.** Only preprints that *did* get published
  appear here. Preprints that never published are invisible to this design.
- **The 2026 slice is young** — recent preprints have had less time to publish,
  so the most recent months under-represent slow-publishing work.
- **A null result is a real result.** A well-powered null across 200k pairs is
  a legitimate metaresearch finding; the project's value doesn't depend on the
  answer being "yes".

---

## Licensing

bioRxiv and medRxiv abstracts are redistributed under the licences the authors
chose, which vary per preprint (many CC-BY, some CC-BY-NC-ND, some "no reuse
without permission"). **Settle the licensing question before publishing the
full corpus anywhere public.** That is why the raw CSV is not committed here
and only a 300-row sample is included for schema inspection.
