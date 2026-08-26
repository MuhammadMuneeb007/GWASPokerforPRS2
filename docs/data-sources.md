# API sources

Every external interface GWASPoker depends on, what it is used for, what it
returned when it was last checked, and what GWASPoker does when it does not
answer.

**Last verified: 2026-08-24.** The `integration` test suite
(`pytest -m integration`) re-checks the contracts recorded here; run it when
upstream behaviour is in doubt.

---

## Summary

| Source | Status | Used for |
| --- | --- | --- |
| GWAS Catalog REST API v2 | Published; `/studies` was returning HTTP 500 during the audit | Study metadata, summary-statistics location |
| GWAS Catalog REST API v1 | Healthy | Fallback for all of the above |
| GWAS Catalog search index (Solr) | Healthy | Free text to EFO term resolution |
| GWAS Catalog Summary Statistics API | **Withdrawn — HTTP 410 Gone** | Nothing; its status is measured and reported |
| GWAS-SSF `-meta.yaml` sidecars (FTP) | Healthy | Structured, authoritative file description |
| EBI FTP over HTTPS | Healthy; honours byte ranges | File listing, probing, downloading |
| NCBI ID converter / doi.org | Healthy | Optional citation resolution |

---

## 1. GWAS Catalog REST API v2

**Base:** `https://www.ebi.ac.uk/gwas/rest/api/v2`
**Docs:** <https://www.ebi.ac.uk/gwas/rest/api/v2/docs>
**OpenAPI:** `https://www.ebi.ac.uk/gwas/rest/api/v2/reference/api-docs`
**Adapter:** `catalog/rest_api.py`

Endpoints GWASPoker uses:

| Endpoint | Purpose |
| --- | --- |
| `GET /v2/studies/{accession_id}` | One study |
| `GET /v2/studies?efo_trait=&full_pvalue_set=&size=&page=` | Studies for a trait |
| `GET /v2/publications/{pubmed_id}` | Publication detail for a study |
| `GET /v2/metadata` | Data release date, EFO version, API version |

Fields consumed from `StudyDto`, mapped in `_study_from_v2`:

```text
accession_id                  -> study_accession
disease_trait                 -> reported_trait
efo_traits[].efo_id/efo_trait -> mapped_traits
initial_sample_size           -> initial_sample_description
replication_sample_size       -> replication_sample_description
discovery_ancestry[]          -> ancestries (stage="initial")
replication_ancestry[]        -> ancestries (stage="replication")
full_summary_stats_available  -> summary_statistics_available
full_summary_stats            -> summary_statistics_location
pubmed_id, snp_count, cohort, genotyping_technologies
```

`full_summary_stats` is the reason v2 is tried first: it is the only API field
that gives the FTP location directly. v1 does not expose it, so GWASPoker falls
back to the documented directory convention (section 5).

Documented constraints, honoured in `http.py` and `config.py`:

* **Rate limit** — 15 queries per second. GWASPoker's default ceiling is 8/s
  (`max_requests_per_second`).
* **Pagination** — default page size 20; responses carry HAL `_links`.
* **Trait queries** — `efo_trait` matches an exact EFO label;
  `show_child_trait=true` includes more specific descendants.

### Observed instability

Every request to `/v2/studies` and `/v2/studies/{accession}` returned:

```json
{"status":500,"error":"Internal Server Error","path":"/gwas/rest/api/v2/studies"}
```

while `/v2/metadata` and `/v2/publications` returned 200 and v1 was fully
healthy. `/v2/efo-traits` returned `406 Not Acceptable — "Content must not be
null!"` for every parameter combination tried.

**GWASPoker's response:** try v2 first, fall back to v1 on any non-404 error,
remember the failure for the rest of the session (`_v2_healthy`), and record
`api_source` on every study so a report says which route produced it. A v2
outage degrades the summary-statistics location to convention-derived; it never
fails the run.

---

## 2. GWAS Catalog REST API v1

**Base:** `https://www.ebi.ac.uk/gwas/rest/api`
**Adapter:** `catalog/rest_api.py`

| Endpoint | Purpose |
| --- | --- |
| `GET /studies/{accession}` | One study |
| `GET /studies/{accession}/efoTraits` | Its ontology annotations |
| `GET /studies/search/findByEfoTrait?efoTrait=` | Studies for an exact EFO label |
| `GET /studies/search/findByDiseaseTrait?diseaseTrait=` | Studies for a reported trait |
| `GET /efoTraits/search/findByEfoTrait?trait=` | Look up an EFO term |

v1 uses camelCase and a different shape from v2; `_study_from_v1` maps
`accessionId`, `diseaseTrait.trait`, `fullPvalueSet`, `initialSampleSize`,
`ancestries[].numberOfIndividuals`, `ancestries[].ancestralGroups[]` and
`publicationInfo.*`.

**Structured ancestry is v1's advantage:** `ancestries[].numberOfIndividuals` is
a per-stage integer, which is the Priority-1 source for sample size in
`metadata/samples.py`.

v1 cannot filter on summary-statistics availability, so `--sumstats-only`
oversamples (`OVERSAMPLE_FACTOR = 8`, capped at `MAX_FETCH = 400`) and filters
client-side. Without that the first page — mostly top-association studies —
crowds out the files GWASPoker exists to triage.

---

## 3. GWAS Catalog search index

**Endpoint:** `GET https://www.ebi.ac.uk/gwas/api/search?q=<text>&max=<n>`

The Solr index behind the website's search box. It returns heterogeneous
documents distinguished by `resourcename`:

* `resourcename: "trait"` — `mappedTrait`, `shortForm` (EFO id), `mappedUri`,
  `termStudyCount`;
* `resourcename: "study"` — `accessionId`, `title`, `fullPvalueSet`;
* `resourcename: "publication"` — `pmid`, `journal`, `authorsList`.

GWASPoker uses it for one thing: turning a phenotype string into EFO terms that
the structured endpoints can accept. This replaces v1's
`fuzz.token_sort_ratio(...) > 50` over a manually downloaded TSV — a term the
website finds is now a term GWASPoker finds.

Also available and documented by the v2 API docs, though not required at
runtime:

* `https://www.ebi.ac.uk/gwas/api/search/downloads/trait_mappings` — TSV of
  `Disease trait / EFO term / EFO URI / Parent term / Parent URI`.

---

## 4. GWAS Catalog Summary Statistics API — withdrawn

**Base:** `https://www.ebi.ac.uk/gwas/summary-statistics/api`
**Adapter:** `catalog/sumstats_api.py`

Every endpoint answers:

```text
HTTP/1.1 410 Gone

This API has been deprecated.
For ways to access summary statistics see:
https://www.ebi.ac.uk/gwas/docs/methods/summary-statistics
```

Confirmed for `/api/`, `/api/associations`, and
`/api/studies/{accession}/associations`. The v2 API documentation states:

> A second API enabling access to data from the full genome-wide summary
> statistics collection is under development.

### Why this matters to GWASPoker

The original specification for this rewrite assumed a working association
endpoint could establish PRS-relevant fields without touching the data file.
That route no longer exists. Two consequences:

1. **It strengthens the case for the tool.** With no field-level API, the only
   ways to learn a file's columns are to download it or to probe it. GWASPoker
   does the latter.
2. **The structured route had to be found elsewhere.** It is the GWAS-SSF
   sidecar (section 5), which turns out to be *better* for triage than the
   association endpoint would have been: it states conformance to a standard
   that fixes the entire mandatory column set, rather than showing a handful of
   example records.

### How GWASPoker reports it

`ApiAvailability.DEPRECATED`, never `NOT_REPRESENTED` and never
`SERVER_ERROR`. The distinction is not pedantic:

| Status | Meaning | Right response |
| --- | --- | --- |
| `DEPRECATED` (410) | Permanently withdrawn | Do not retry; use another route |
| `NOT_REPRESENTED` (404) | This study is not served here | Do not retry for this study |
| `SERVER_ERROR` (5xx) | The server is unwell | Retrying later may work |
| `TIMEOUT` | The request did not complete | Retrying may work |

GWASPoker still issues the query, once per study, so that the endpoint's status
is *measured* per study rather than asserted. `test_integration.py` asserts the
410 explicitly: if it ever changes, that test fails and this document needs
revisiting.

---

## 5. GWAS-SSF metadata sidecars

**Location:** alongside every modern data file on the FTP site
**Standard:** <https://github.com/EBISPOT/gwas-summary-statistics-standard>
**Parser:** `catalog/sumstats_api.py::parse_ssf_metadata`

Each data file `X` is accompanied by `X-meta.yaml`, roughly 700 bytes:

```yaml
gwas_id: GCST90992810
genome_assembly: GRCh37
coordinate_system: 1-based
samples:
  - sample_ancestry_category: [European]
    sample_size: 69039
    case_control_study: false
data_file_name: GCST90992810.tsv.gz
file_type: GWAS-SSF v1.0
data_file_md5sum: 5a486de3ea7f4e7d1a48df6c546ff32b
is_harmonised: false
```

Fields GWASPoker uses:

| Field | Use |
| --- | --- |
| `file_type` | `GWAS-SSF v1.0` or `pre-GWAS-SSF` — the branch point, and the benchmark's `ssf_status` stratum |
| `genome_assembly` | Reported; needed to know whether liftover is required |
| `samples[].sample_size`, `case_count`, `control_count` | Priority-1 sample counts |
| `data_file_md5sum` | Download verification |
| `is_harmonised` | Corroborates the file-selection decision |

### Why `file_type` settles PRS readiness

GWAS-SSF v1.0 mandates these columns, in this order:

```text
chromosome  base_pair_location  effect_allele  other_allele
<effect>  standard_error  effect_allele_frequency  p_value
```

where `<effect>` is `beta`, `odds_ratio`, `hazard_ratio`, or — documented as a
last resort when none of those is available — `z-score`. Additional columns may
follow. Omitted values are written `#NA`.

That set satisfies every PRS requirement in
[`mapping.md`](mapping.md). So when a sidecar declares
`GWAS-SSF v1.0`, readiness follows from the declaration and **no byte of the
data file needs to move**. `assess --force-probe` exists to test that inference
against the observed header; on the studies checked so far the two agree
exactly.

A `pre-GWAS-SSF` declaration guarantees nothing about the columns, so the probe
runs.

---

## 6. EBI FTP over HTTPS

**Base:** `https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics`
**Adapters:** `download/resolver.py`, `probe/remote.py`, `download/downloader.py`

### Directory convention

Accessions are grouped into blocks of 1000:

```text
summary_statistics/
  GCST90038001-GCST90039000/
    GCST90038646/
      GCST90038646_buildGRCh37.tsv              1.2G   raw submission
      GCST90038646_buildGRCh37.tsv-meta.yaml    650    GWAS-SSF metadata
      md5sum.txt                                134    checksums
      harmonised/
        33959723-GCST90038646-EFO_0003821.h.tsv.gz            378M
        33959723-GCST90038646-EFO_0003821.h.tsv.gz-meta.yaml  776
        33959723-GCST90038646-EFO_0003821-Build37.f.tsv.gz    218M
        md5sum.txt                                            384
```

`accession_block()` derives the block; the result is always verified by an
actual request before use. A study with no full summary statistics has no
directory, and the resolver reports a clean `file_not_found`.

### Harmonised file naming

```text
<PMID>-<GCST>-<EFO>.h.tsv.gz            fully harmonised
<PMID>-<GCST>-<EFO>-Build37.f.tsv.gz    format-harmonised only
```

`.h.` outranks `.f.`, which outranks the raw file when `--harmonised auto`
(the default). Size is a tiebreaker and cannot overturn a naming decision.

### Byte ranges

Verified on `ftp.ebi.ac.uk`:

```text
HEAD .../33959723-GCST90038646-EFO_0003821.h.tsv.gz
  200  Content-Length: 396130130  Accept-Ranges: bytes

GET  same URL, Range: bytes=0-65535
  206  Content-Range: bytes 0-65535/396130130   65536 bytes in ~1.0 s
```

64 KB of that gzip stream inflates to about 240 KB of text — several hundred
rows, far more than a header needs. A `--probe-bytes 262144` probe of the same
file avoids 99.93% of the transfer.

When a server ignores the Range header (`200` instead of `206`), GWASPoker
streams and closes the connection at the byte limit itself, and records
`range_supported: false`. The bound is always on bytes, never on time.

### Checksums

`md5sum.txt` lists `<md5>  <filename>` per directory. GWASPoker verifies the
**data file** after download and refuses to present an unverified file under its
real name — it stays as `.part`.

One caveat found during the audit: for `GCST90038646` the `md5sum.txt` entry for
the `-meta.yaml` *sidecar* is stale (the sidecar was regenerated afterwards),
while the entry for the data file is correct and matches `data_file_md5sum`
inside the sidecar. GWASPoker verifies the data file, which is the one that
matters.

---

## 7. Citation resolution (optional)

| Endpoint | Purpose |
| --- | --- |
| `https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids=<pmid>&format=json` | PMID to DOI |
| `https://doi.org/<doi>` with `Accept: application/x-bibtex` | DOI to BibTeX |

Preserved from v1's Module 2. Never on the critical path: a failure is logged
and the field stays `None`.

---

## 8. What GWASPoker does when a source does not answer

`failures.py` maps every outcome to a category. The rules that matter
scientifically:

* **A single failed request never means "not represented."** Absence is claimed
  only on an explicit 404 from a healthy endpoint.
* **410 is not 404.** A withdrawal is permanent and documented; a 404 is
  study-specific.
* **A 5xx is never recorded as a data fact.** It is `api_error`, and the
  fallback route runs.
* **`unknown` beats a guess.** Any field that cannot be established is `None` in
  the model and a dim `unknown` in output — never `0`, never `-`, never blank.

---

## 9. Re-verifying this document

```bash
pytest -m integration          # asserts every contract above
gwaspoker assess GCST90038646 --target prs -vv
```

If an integration test fails, upstream has changed. Update the adapter
(`catalog/rest_api.py` or `catalog/sumstats_api.py`) and this file together —
they are meant to stay in step.
