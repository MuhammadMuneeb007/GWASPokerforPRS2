# Example: searching for studies

All output below is copied from real runs against the live GWAS Catalog on
2026-08-24. Counts and accessions will change as the Catalog grows.

---

## Basic search

```bash
gwaspoker search --trait migraine --limit 8
```

```text
GWAS Catalog studies for migraine
GCST          Trait                                Population        N       Cases  Controls  Sum stats  Year      PMID
GCST011064    Migraine and/or systolic blood pre…  European   1,094,154    59,674   316,078     no      2020  32632093
GCST011065    Migraine and/or pulse pressure       European   1,094,154    59,674   316,078     no      2020  32632093
GCST011063    Migraine and/or diastolic blood pr…  European   1,094,154    59,674   316,078     no      2020  32632093
GCST010091    Endometriosis or migraine            European,      411,051   46,262   364,789     no      2020  32121467
                                                   East Asian
GCST003281    Migraine                             European     375,752    59,674   316,078     no      2016  27322543
GCST004346    Migraine or coronary artery disease  European,    230,881    59,773   171,108     no      2017  28957430
                                                   NR
GCST002346    Migraine                             European     118,710    23,285    95,425     no      2013  23793025
GCST001631    Migraine                             European      23,230     5,122    18,108     no      2011  21666692

8 study/studies. Sample counts carry provenance; run with --format json to see it.
```

Note `Sum stats: no` on all of these — they publish top associations only. That
is exactly the filter you usually want next.

---

## Only studies with full summary statistics

```bash
gwaspoker search --trait migraine --population European --sumstats-only --limit 10
```

```text
GWAS Catalog studies for migraine in European
GCST          Trait                               Population        N   Cases  Controls  Sum stats  Year      PMID
GCST90271641  Migraine                            European    513,266  26,052   487,214     yes     2023  37415806
GCST90043745  Migraine (PheCode 340)              European    456,348   1,488   454,860     yes     2021  34737426
GCST90475837  Migraine (PheCode 340)              European    437,667  31,836   405,831     yes     2024  39024449
GCST90435920  Migraine (PheCode 340)              European    401,650   2,870   398,780     yes     2018  30104761
GCST90079826  ICD10 G43.9: Migraine, unspecified  European    387,898   3,383   384,515     yes     2021  34662886
GCST90079827  ICD10 G43: Migraine                 European    378,172   8,426   369,746     yes     2021  34662886
GCST90081731  Migraine (Gene-based burden)        European    331,754  14,131   317,623     yes     2021  34662886
GCST90077745  Migraine                            European    331,754  14,131   317,623     yes     2021  34662886
GCST90081732  ICD10 G43: Migraine (Gene-based     European    329,052  11,475   317,577     yes     2021  34662886
              burden)
GCST90475546  Migraine headaches                  European    315,668  28,635   287,033     yes     2024  39024449
```

The GWAS Catalog REST API v1 cannot filter on summary-statistics availability
server-side, so GWASPoker oversamples and filters client-side. Without that the
first page — mostly top-association studies — would crowd out the files you can
actually use.

---

## Where the numbers came from

```bash
gwaspoker search --trait migraine --limit 5 --show-provenance
```

```text
                Sample-count provenance
 GCST          N source        Cases source  Controls source
 GCST011064    structured_api  regex         regex
 GCST011065    structured_api  regex         regex
 GCST003281    structured_api  regex         regex
 GCST002346    structured_api  regex         regex
 GCST001631    structured_api  regex         regex
```

`structured_api` means the Catalog's own `ancestries[].numberOfIndividuals`
field. `regex` means the count was parsed from the free-text
`initialSampleDescription`, for example:

```text
"14,131 European ancestry cases, 317,623 European ancestry controls"
   -> cases = 14,131   controls = 317,623   (both regex)
```

A value that could not be established anywhere is `unknown` — never `0`, never
a blank that reads as zero. That distinction is why the provenance columns
exist.

---

## Machine-readable output

```bash
gwaspoker search --trait migraine --population European \
    --sumstats-only --limit 20 \
    --output search_results.csv --format csv
```

`search_results.csv` columns:

```text
study_accession, reported_trait, mapped_trait, efo_ids, population,
sample_size, sample_size_source, cases, cases_source, controls,
controls_source, summary_statistics_available, summary_statistics_location,
initial_sample_description, replication_sample_description, genome_build,
study_year, pubmed_id, first_author, journal, publication_title,
association_count, api_source, ancestry_match_score
```

JSON output additionally carries the full provenance block:

```bash
gwaspoker search --trait migraine --output search_results.json --format json
```

```json
{
  "report_type": "search",
  "provenance": {
    "environment": {
      "gwaspoker_version": "2.0.0",
      "python_version": "3.13.2",
      "timestamp_utc": "2026-08-24T15:32:11+00:00",
      "catalog_data_release": "2026-08-22",
      "catalog_api_version": "2.0",
      "efo_version": "v3.93.0"
    },
    "configuration": { "probe_bytes": 262144, "...": "..." }
  },
  "results": [ { "study_accession": "GCST90271641", "...": "..." } ]
}
```

The `catalog_data_release` and `efo_version` fields tie a result set to a
specific Catalog release, which is what makes a search reproducible later.

---

## Ancestry matching

`--population` is matched against the Catalog's controlled ancestry vocabulary,
not by string similarity:

| Requested | Study ancestry | Score | Reason |
| --- | --- | --- | --- |
| European | European | 1.00 | exact category match |
| European | European, East Asian | 0.75 | multi-ancestry including the request |
| European | NR | 0.40 | not reported; a match cannot be excluded |
| European | East Asian | 0.00 | definite mismatch |

Synonyms resolve to the same category, so `Caucasian`, `european` and `EUR` all
find European studies. A study reporting `NR` is scored 0.40 rather than
excluded, because "not reported" is not the same as "does not match".

---

## Sample-size extraction with the optional LLM

```bash
pip install -e ".[llm]"
gwaspoker search --trait migraine --llm
```

Only reached when the structured API and the regex layer have both failed. The
model is downloaded on first use, cached once per process, and its answers are
marked `llm` with the model's own confidence. They never overwrite a structured
or regex value.

Off by default; `--no-llm` is the explicit form.
