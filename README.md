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

Stage 2 works end to end and the join rate has been **measured on a 200-DOI
random sample: 75.0%** (150 `ok`, 46 `not_found`, 4 `no_abstract`, 0 `failed`),
stable across every year from 2019 to 2026.

**An earlier session quoted ~93% from a 30-DOI spot check. That does not
reproduce — do not use it.** If it appears in the project pitch or the PDF,
correct it to 75%.

Practically: ~75% of 200,364 is roughly **150,000 usable pairs**, which is
ample. But ~50,000 pairs go missing, and that loss may not be random —
`collect_published.py` records an explicit `status` per DOI precisely so
missingness can be characterised rather than silently dropped. Check whether
`not_found` correlates with journal or field before treating the remainder as
a fair sample.

Next step if you want those rows back: a Crossref fallback for `not_found`
DOIs — a separate stage 2b that disturbs nothing upstream.

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
