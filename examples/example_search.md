# Example: searching for studies

All output below is copied from real runs against the live GWAS Catalog on
2026-08-24. Counts and accessions will change as the Catalog grows.

---

## Basic search

```bash
gwaspoker search --trait migraine --population European --limit 8
```

```text
GWAS Catalog studies for migraine in European
GCST          Trait                          Population        N   Cases  Controls  File  API  Harmonised  GWAS-SSF  Year      PMID
GCST90473326  ICD10 G43: Migraine            European    458,440  25,393   433,047  yes   yes     yes        yes     2025  40770095
GCST90083812  ICD10 G43.9: Migraine,         European    387,898   3,383   384,515  yes   yes      no         no     2021  34662886
              unspecified (Gene-based
              burden)
GCST90079826  ICD10 G43.9: Migraine,         European    387,898   3,383   384,515  yes   yes     yes         no     2021  34662886
              unspecified
GCST90079827  ICD10 G43: Migraine            European    378,172   8,426   369,746  yes   yes     yes         no     2021  34662886
GCST90671940  Migraine                       European    341,050  10,881   330,169  yes   yes      no        yes     2022  35115687
GCST90081731  Migraine (Gene-based burden)   European    331,754  14,131   317,623  yes   yes      no         no     2021  34662886
```

Four columns say what it will take to actually use each study:

| Column | Means |
| --- | --- |
| **File** | A summary-statistics data file is published |
| **API** | The GWAS-SSF `-meta.yaml` sidecar is retrievable, so the structured route can describe the file and `assess` needs **no probe** |
| **Harmonised** | A `harmonised/` product is published alongside the raw submission |
| **GWAS-SSF** | The file declares conformance to GWAS-SSF v1.0, so its mandatory column set is guaranteed |

Each is `yes`, `no`, or `?` when the fact could not be established. `?` is never
rendered as a blank — a blank cell reads as "no" to most people, and the two are
different facts.

Reading the table above: `GCST90473326` is the best candidate — it has a file, a
readable sidecar, a harmonised product, *and* declares GWAS-SSF, so `assess` will
return a verdict without transferring any data. The `(Gene-based burden)` studies
have no harmonised product, so they will need liftover if your genotypes are on a
different build.

These four columns cost one FTP directory listing plus one sidecar fetch per
study — about a second each. Pass `--no-check-files` to skip them (they show `?`)
when you only want the trait and sample counts:

```bash
gwaspoker search --trait migraine --no-check-files    # returns immediately
```

---

## Only studies with full summary statistics

```bash
gwaspoker search --trait migraine --population European --sumstats-only --limit 10
```

```text
GWAS Catalog studies for migraine in European
GCST          Trait                          Population        N   Cases  Controls  File  API  Harmonised  GWAS-SSF  Year      PMID
GCST90473326  ICD10 G43: Migraine            European    458,440  25,393   433,047  yes   yes     yes        yes     2025  40770095
GCST90079826  ICD10 G43.9: Migraine,         European    387,898   3,383   384,515  yes   yes     yes         no     2021  34662886
              unspecified
GCST90079827  ICD10 G43: Migraine            European    378,172   8,426   369,746  yes   yes     yes         no     2021  34662886
GCST90671940  Migraine                       European    341,050  10,881   330,169  yes   yes      no        yes     2022  35115687
GCST90077745  Migraine                       European    331,754  14,131   317,623  yes   yes     yes         no     2021  34662886
```

With `--sumstats-only`, every row has `File = yes` by construction, so the
interesting columns become API, Harmonised and GWAS-SSF.

The GWAS Catalog REST API v1 cannot filter on summary-statistics availability
server-side, so GWASPoker oversamples and filters client-side. Without that the
first page — mostly top-association studies — would crowd out the files you can
actually use.

---

## Where the numbers came from

```bash
gwaspoker search --trait migraine --limit 5 --show-provenance --no-check-files
```

```text
                Sample-count provenance
 GCST          N source  Cases source  Controls source
 GCST90083812  regex     regex         regex
 GCST90079826  regex     regex         regex
 GCST90079827  regex     regex         regex
 GCST90083813  regex     regex         regex
 GCST90081731  regex     regex         regex
```

`structured_api` means the Catalog's own `ancestries[].numberOfIndividuals`
field, and `ssf_metadata` means the count came from the GWAS-SSF sidecar that the
availability check fetched anyway. `regex` means the count was parsed from the
free-text `initialSampleDescription`, for example:

```text
"14,131 European ancestry cases, 317,623 European ancestry controls"
   -> cases = 14,131   controls = 317,623   (both regex)
```

A value that could not be established anywhere is `unknown` — never `0`, never
a blank that reads as zero. That distinction is why the provenance columns
exist.

Running without `--no-check-files` can *improve* these: the GWAS-SSF sidecar is
fetched for the availability columns anyway, and when it carries `sample_size`,
`case_count` or `control_count` those authoritative values replace the regex
ones and the source becomes `ssf_metadata`.

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
controls_source, file_available, api_available, harmonised_available,
ssf_status, summary_statistics_available, summary_statistics_location,
resolved_file_name, resolved_file_size, initial_sample_description,
replication_sample_description, genome_build, study_year, pubmed_id,
first_author, journal, publication_title, association_count, api_source,
ancestry_match_score
```

`ssf_status` carries the full string (`GWAS-SSF` / `pre-GWAS-SSF`) rather than
the table's yes/no, and `resolved_file_name` / `resolved_file_size` record which
file the availability check actually looked at.

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
