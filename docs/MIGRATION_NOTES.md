# Migration notes: GWASPoker v1 → GWASPoker v2

This document records the audit of the original implementation
(`../GWASPokerforPRS`, untouched) and the decisions taken when rebuilding it as
the installable `gwaspoker` package.

Audit date: 2026-08-24. Live API behaviour recorded in
[`API_SOURCES.md`](API_SOURCES.md) was verified on the same date.

---

## 1. Inventory of the original project

| File | Lines | Role | Fate in v2 |
| --- | --- | --- | --- |
| `Module 0 - GWAS File Analysis.ipynb` | 5 code cells | Exploratory word clouds / frequency plots over a downloaded metadata export | **Dropped.** Research notebook, not production behaviour. Its dependency block (`sklearn`, `nltk`, `wordcloud`, `matplotlib`) is not carried over. |
| `Module1-SearchPhenotypeandPopulation.py` | 197 | Fuzzy phenotype/population search over a manually downloaded TSV; ELECTRA QA extraction of cases/controls/N | **Rebuilt** as `gwaspoker search` (`catalog/discovery.py`, `metadata/samples.py`, `metadata/llm_extractor.py`). |
| `Module2-Search_Poke_Normalize_Scan.py` | 1092 | Alias tables; FTP HTML scraping; 10-second `wget` "poke"; delimiter/quote handling; Bootstrap HTML report; PMID→DOI→BibTeX | **Split** across `catalog/`, `download/resolver.py`, `probe/`, `mapping/`, `reporting/html.py`. |
| `Module3-DownloadGWAS.py` | 57 | `os.system("wget ...")` full download driven by a two-column CSV | **Rebuilt** as `gwaspoker download` (`download/downloader.py`). |
| `Module4-ExtractGWAS.py` | 722 | Duplicate of Module 2's alias tables + `poker()`; archive extraction; emits `transform.txt` / `Output.py` | **Rebuilt** as `gwaspoker extract` (`processing/`). |
| `Module5-ListPRSColumns.py` | 703 | Near-identical duplicate of Module 4; lists PRS columns for a local file; HuggingChat code generation | **Rebuilt** as `gwaspoker scan` (`readiness/`, `mapping/`). |
| `environment.txt` | 332 | `conda create --file` export, `# platform: linux-64` | **Replaced** by `pyproject.toml`, `requirements.txt`, `environment.yml` (platform-independent). |

`Module4` and `Module5` differ only in their `__main__` block — `diff` reports
changes confined to argument parsing and output paths. Their ~180 lines of alias
tables and their `poker()` / `detect_delimitor_gz()` / `remove_quotes()` helpers
are byte-identical to Module 2's.

---

## 2. Functionality worth preserving

These behaviours were genuinely useful and are carried into v2, in improved form.

1. **Curated column-alias vocabulary.** The 14 alias lists represent real
   curation effort over the 60,400 catalogue files analysed for the original
   study. Migrated wholesale into `src/gwaspoker/mapping/aliases.yaml`
   (defects corrected — see section 4).
2. **Canonical PRS column concepts.** The `CHR / SNP / BP / A1 / A2 / N / P /
   INFO / MAF / BETA / SE / OR / Z / D` target vocabulary is a sound PRS-tool
   target set. It survives as the `prs_tool_symbol` field of each canonical
   concept in `aliases.yaml`.
3. **Pre-download inspection.** The central idea — look at the header before
   committing to a multi-gigabyte transfer — is the scientific contribution and
   is now the core of `gwaspoker probe`.
4. **FTP directory enumeration and `harmonised/` preference.** Preserved and
   made explicit in `download/resolver.py`.
5. **Fetching the sibling `readme` file.** Generalised: v2 fetches the
   GWAS-SSF `-meta.yaml` sidecar and `md5sum.txt`, which are far more
   informative than the README (see section 7).
6. **Sample-size / case / control extraction from free-text descriptions.**
   Preserved as a *fallback* layer behind structured API fields.
7. **Broad archive support** (`.gz`, `.zip`, `.tar`, `.tar.gz`, `.xlsx`,
   `.ma`, `.assoc`, `.meta`, `.tbl`, `.linear`, `.logistic`). The extension set
   is preserved in `processing/formats.py`.
8. **HTML report per study.** Preserved as `reporting/html.py`, without the
   CDN `<link>` (offline use and archival need a self-contained page).
9. **PMID → DOI → BibTeX resolution.** Preserved in `catalog/rest_api.py` as an
   opt-in citation helper.
10. **Explicit "unidentified columns" listing.** Preserved: `MappingResult`
    always reports columns that mapped to `unknown`.

---

## 3. Duplication removed

| Duplicated thing | Occurrences in v1 | v2 location |
| --- | --- | --- |
| 14 column-alias lists (~180 lines) | 3 (Modules 2, 4, 5) | `mapping/aliases.yaml` (single YAML resource) |
| `poker()` extraction driver (~190 lines) | 3 | `processing/extract.py` |
| `detect_delimiter()` / `detect_delimitor_gz()` / `detect_delimitor_gz2()` | 6 variants | `probe/header.py::detect_delimiter` |
| `remove_quotes()` / `remove_quotes2()` | 4 | `processing/normalize.py` (opt-in, non-destructive) |
| `create_directory()` | 4 | `pathlib.Path.mkdir(parents=True, exist_ok=True)` |
| `list_files_with_sizes()` / `find_largest_file()` | 3 | `download/resolver.py` ranking |
| `is_string_in_list()` | 3 | `mapping/mapper.py` |
| `import pandas as pd` inside one file | 4x in Module 1, 5x in Module 2 | one import per module |

Module 1's import block alone loads `sklearn` (three submodules), `nltk`,
`matplotlib` (four imports), `BeautifulSoup`, `codecs` and `transformers` at
module scope, for a script that never calls any of the clustering imports.
v2 imports `transformers` lazily and does not depend on `sklearn`, `nltk`,
`matplotlib` or `bs4` at all.

---

## 4. Defects found

### 4.1 Implicit string concatenation from missing commas (data corruption)

Python concatenates adjacent string literals. Each of the following produced a
**silently malformed alias** that can never match a real column, and
simultaneously **lost** the two aliases it was built from. All appear in three
files:

| List | Source (v1) | Resulting corrupt alias |
| --- | --- | --- |
| `chromosome_list` | `'name'` then `'chromosome_position_reference_allele_other_allele_b37'` | `namechromosome_position_reference_allele_other_allele_b37` |
| `effect_allele_list` | `'effect_allele_all'` then `'minorallele'` | `effect_allele_allminorallele` |
| `alternative_allele_list` | `'nea'` then `'alt_allele'` | `neaalt_allele` |
| `alternative_allele_list` | `'noncoded_allele'` then `'noneffect_allele'` | `noncoded_allelenoneffect_allele` |
| `p_value_list` | `'neg_log_10_p_value'` then `'p'` | `neg_log_10_p_valuep` |

v2 stores aliases in YAML, where this failure mode cannot occur, and
`tests/test_mapping.py::test_no_suspicious_concatenated_aliases` asserts that no
alias is the concatenation of two other known aliases.

### 4.2 `p_value_list` is a semantically incorrect alias set

`neg_log_10_p_value` is **not** a p-value: it is -log10(p). Mapping it to
`p_value` and handing the result to a PRS tool silently inverts the significance
scale. v2 gives it its own canonical concept, `neg_log10_p_value`, which
satisfies the "statistical significance" readiness requirement but is never
mapped onto `p_value`.

`p_value_list` also contains roughly 250 trait-specific aliases (`ala_pval`,
`xxl_vldl_tg_pval`, and so on) from one metabolomics resource. These follow the
regular pattern `<analyte>_pval`; v2 keeps a representative subset and covers
the remainder with the `*_pval` suffix heuristic in `mapping/mapper.py`
(Layer 3), rather than enumerating them.

### 4.3 Cross-contaminated allele lists

`effect_allele_list` contains `'allele_0'`, `'allele0'` and `'a0'`;
`alternative_allele_list` contains `'allele1'`, `'allele_1'` **and** `'a1'`.
So `a1` and `allele1` appear in *both* lists, and `is_string_in_list()` reports
the same column as both the effect and the other allele. v2 resolves this: `a1`
and `allele1` map to `effect_allele` (the dominant PLINK/METAL convention) and
`a0` / `allele0` map to `other_allele`, with the conflicting entries removed.
Genuinely ambiguous cases are reported with reduced confidence rather than
duplicated across concepts.

`effect_allele_list` also contains `'minorallelefrequency'` — a frequency
column, not an allele column. Moved to `effect_allele_frequency`.

### 4.4 `N_list` conflates three distinct concepts

`N_list` mixes total sample size (`n`, `total_n`), case counts (`ncase`,
`n_cases`, `num_cases`) and control counts (`ncontrol`, `n_controls`). A PRS
tool told that `n_cases` is `N` will use the case count as the total. v2 splits
these into `sample_size`, `cases` and `controls`.

`N_list` also contains `'weight'`, which is a meta-analysis weight, not N.
Dropped.

### 4.5 `beta_list` contains a p-value column

`beta_list` begins with `'p_bolt_lmm_inf'` — the BOLT-LMM infinitesimal-model
**p-value**. The original `Output-Module4-Migraine-Code.py` committed to the
repository shows the consequence: it emits
`'beta': 'BETA', 'p_bolt_lmm_inf': 'BETA'`, mapping a p-value column onto the
effect size, with `p_bolt_lmm_inf` overwriting `beta` in the rename dictionary.
v2 maps `p_bolt_lmm_inf` to `p_value`.

Similarly `beta_list` contains `'n_effective'` (a sample size) and
`'betazscale'` (also present in `zscore`). Both corrected.

### 4.6 Fragment aliases cause false positives

`se_list` contains `'standard'` and `'_error'`; `or_list` contains `'odds'`;
`base_pair_list` contains `'base'`, `'pair'` and `'b'`; `effect_allele_list`
contains `'e'`; `p_value_list` contains `'_value'`. Single fragmentary tokens
match unrelated columns. These were dropped; the truncation artefacts they were
evidently meant to catch (`tandard_error`, `andard_error`, `dard_error`) are
retained because they are unambiguous.

### 4.7 `Module2` exits before doing any work

Line 227 of `Module2-Search_Poke_Normalize_Scan.py` is a bare `exit(0)` at
module scope, immediately after the alias-count printout. Everything below it —
`poker()`, `scrape()`, the whole `__main__` block — is unreachable. As committed,
Module 2 prints 14 counts and terminates.

### 4.8 Set-based header comparison loses order and duplicates

`unidentifiedcols = set(gwascols) - set(allcolumns)` discards column order and
collapses duplicate column names. Header order matters for positional readers
and for the benchmark's *exact ordered header match* metric. v2 keeps headers as
`tuple[str, ...]` throughout and never converts to `set` for equality.

### 4.9 Blanket string replacement corrupts scientific values

```python
modified_content = content.replace('"', '').replace(':', '_').replace('\t', ',')
```

applied to the whole file (`remove_quotes2`). This rewrites *data*, not just
delimiters:

* `:` to `_` destroys `chr:pos` variant identifiers (`1:12345:A:G` becomes
  `1_12345_A_G`) and any ISO timestamp;
* `\t` to `,` corrupts every field that legitimately contains a comma once the
  quotes have already been stripped;
* stripping `"` before converting delimiters removes the only protection
  embedded delimiters had.

`extract_number()` in Module 1 does the same at value level:
`input_string.replace(",", "").replace(".", "")` turns `1.5e-8` into `15e-8` and
`0.75` into `075`.

v2 never rewrites data to make a parser happy. `processing/normalize.py`
performs only declared, reversible transformations, and every one is reported in
the `transformations` list of the extraction result.

### 4.10 Bare `except:` and `except: pass` throughout

Twenty-three bare `except` clauses across the five modules, several with `pass`
bodies (`Module2` lines 913 and 1071). `Module1.extract_number` returns `0` — a
*number* — on any failure, so a parse error is indistinguishable from a real
zero. v2 uses `FailureCategory` (see `failures.py`), specific exception types,
and never returns a plausible-looking value to signal failure; `None` and
`"unknown"` are used instead.

### 4.11 "Largest file wins" file selection

`find_largest_file()` and `df['Sizes_numeric'].idxmax()` pick the largest entry
in the FTP directory. Counterexamples encountered during the audit:

* `GCST006867/` contains `Xue_et_al_T2D_META_Nat_Commun_2018.pdf` alongside the
  data file — a large PDF would win;
* inside `harmonised/`, `*.h.tsv.gz` (378 MB) and `*.f.tsv.gz` (218 MB) are both
  valid but semantically different products;
* `GCST90038646/` contains a 1.2 GB *uncompressed* raw TSV and a 378 MB
  harmonised gzip — largest is not preferred.

v2 ranks candidates on filename convention, then extension, then size, and
records `selection_reason` for every choice.

### 4.12 Fixed fuzzy threshold of 50 as the primary search

`fuzz.token_sort_ratio(phenotype, reportedTrait) > 50` over a manually
downloaded TSV. A threshold of 50 on `token_sort_ratio` admits substantial
noise, and the approach requires the user to have first downloaded
`summary_statistics_table_export.tsv` by hand — the script fails with
`FileNotFoundError` otherwise. v2 queries the ontology (EFO) through the
catalogue's own trait index; fuzzy matching survives only as an optional
tiebreaker over already-retrieved structured results.

### 4.13 QA pipeline rebuilt for every row

`questionanswer()` calls `pipeline('question-answering', model=...)` on **every
invocation**, and `searchphenotypeandpopulation()` calls it three times per row
via `DataFrame.apply`. For 100 studies that is 300 constructions of a
335 M-parameter model. v2 caches one pipeline per process
(`metadata/llm_extractor.py::_get_pipeline`).

`getmeanswers_cases_controls` also gates on
`"case" in row and "control" in row`, so a description reading
`"12,000 individuals with migraine"` is never examined for a case count.

### 4.14 Hard-coded credentials placeholder

`Module5` contains `Login("YOUR HUGGING CHAT EMAIL", "YOUR HUGGING CHAT PASSWORD")`
and unconditionally logs in to HuggingChat to generate pandas code, writing
cookies to `./cookies_snapshot`. This makes the module unusable without an
account and sends study metadata to a third party. v2 generates the mapping code
locally and deterministically (`gwaspoker scan --emit-code`); no account, no
network call, no credentials.

### 4.15 Other

* `rename_file_ending_with_digit()` renames `*.gz.1` to `*.gz`, silently
  overwriting the original when both exist (`os.rename` raises on Windows; on
  Linux it clobbers). v2's downloader never creates `.1` duplicates — it resumes
  or refuses without `--overwrite`.
* `scrape()` reassigns `newurl = url = url + os.sep + loop` inside a loop, so
  each iteration appends to the *already-appended* URL, producing
  `.../sub1//sub2/` after two iterations.
* `convert_to_numeric()` treats `'-'` and any unit-less size as `0`, so a
  directory whose sizes are all reported in bytes ranks every file at 0 and
  `idxmax()` returns the first row.
* `filename = largest_file_path.split("/")[3:]` assumes a path depth of exactly
  three and `/` separators — broken on Windows and in any other working
  directory.
* Module 2 writes `Information.csv`, then re-reads `os.listdir` to decide
  whether to re-scrape, using `list.remove()` inside `try/except` as an `in`
  test.
* `detect_delimiter()` opens files in text mode with the platform default
  encoding, which is `cp1252` on this Windows machine and `utf-8` on the Linux
  environment the scripts were written for.

---

## 5. Linux-only shell dependencies removed

Every one of these was invoked through `os.system` or
`subprocess.run(..., shell=True)` and is unavailable or behaves differently on
Windows:

| v1 invocation | v2 replacement |
| --- | --- |
| `timeout -s KILL 10 wget -q <url> -P <dir>` | bounded HTTP `Range` request — `probe/remote.py` |
| `wget <url> -P <dir>` | streaming `requests` download with resume — `download/downloader.py` |
| `bash -c '7z x ... -o...'` | `zipfile` — `processing/extract.py` |
| `bash -c 'tar -xvf ...'` and `tar -xvzf` | `tarfile`, with path-traversal filtering |
| `bash -c 'gunzip -c ... | head -n 100 > ...'` | `gzip` / `zlib.decompressobj(31)` |
| `bash -c 'gunzip -c ... | gunzip -c | ...'` (double gzip) | detected by magic-byte re-inspection — `probe/compression.py` |
| `zcat ... | head -n 100 > ...` | same |
| `bash -c 'cat ... | head -n 100 > ...'` | bounded `read()` |
| `os.system("rm -rf wget-log*")` | no log files are created |

`subprocess` is not imported anywhere in `src/gwaspoker/`; a test asserts this.

Two further portability problems are fixed by construction: the original writes
paths with a mixture of `os.sep` and hard-coded `/`, and `Results/` shows
directory names derived from trait strings without sanitising characters that
are illegal on NTFS.

---

## 6. Inputs and outputs

### v1

| Module | Input | Output |
| --- | --- | --- |
| 0 | `summary_statistics_table_export.tsv` (manual download) | `*.csv` frequency tables, PNG word clouds |
| 1 | same TSV; `--phenotype`, `--population` | `<phenotype>.csv` |
| 2 | manually edited `<phenotype>.csv`; needs `summaryStatistics`, `accessionId`, `reportedTrait`, `pubmedId` columns | `<phenotype>_output.html`, `allgwas/<GCST>/Information.csv`, partial data files |
| 3 | two-column CSV (`Name`, `Download Link`); `--indexer` | full file in `./<Name>/` |
| 4 | same CSV; `--indexer` | `gwas.csv.modified`, `transform.txt`, `Output.py` |
| 5 | `--gwasfile` (CSV only) | `transform.txt`, `transform1.txt`, `Output.py` |

The v1 chain requires **three manual steps**: download the metadata export,
hand-edit Module 1's output, and hand-write Module 3's input CSV.

### v2

No manual step. `gwaspoker` resolves everything from the accession or trait, and
writes named artefacts:

| Command | Input | Output |
| --- | --- | --- |
| `search` | `--trait`, `--population` | table / `search_results.csv` / `.json` |
| `probe` | accession or URL | table / `probe_results.json` |
| `assess` | accession or URL | table / `prs_assessment.csv` / `.json` |
| `scan` | local file, URL or accession | table / JSON |
| `download` | accession or URL | data file, `-meta.yaml`, checksum verification |
| `extract` | local file | normalised TSV plus transformation report |
| `run` | `--trait` | ranked table / `gwaspoker_report.html` |
| `benchmark` | manifest CSV | metrics JSON/CSV |

`Output-Module1-migraine.csv` columns map onto the v2 `Study` model as:
`accessionId` to `study_accession`, `reportedTrait` to `reported_trait`,
`efoTraits` to `mapped_traits`, `discoverySampleAncestry` to
`discovery_ancestry`, `summaryStatistics` to `summary_statistics_location`,
`initialSampleDescription` to `initial_sample_description`, and
`CASES` / `CONTROLS` / `SAMPLES` to a `SampleCounts` object carrying provenance.
`ssApiFlag` is superseded — see section 7.

---

## 7. Findings that change the v2 design

Three live-API facts, verified 2026-08-24, materially affect the architecture.
They are documented in full in [`API_SOURCES.md`](API_SOURCES.md).

1. **The GWAS Catalog Summary Statistics REST API is deprecated.**
   `https://www.ebi.ac.uk/gwas/summary-statistics/api/...` returns
   **HTTP 410 Gone** with the body *"This API has been deprecated. For ways to
   access summary statistics see: .../docs/methods/summary-statistics"*.
   The v2 API documentation states that a replacement "is under development".
   GWASPoker therefore cannot rely on that API for field-level assessment.
   `catalog/sumstats_api.py` detects the 410 explicitly and reports
   `availability = "deprecated"` — it does **not** report "unavailable", which
   would wrongly suggest a transient failure.

2. **The GWAS-SSF `-meta.yaml` sidecar replaces it for triage purposes.**
   Each study directory carries `<data_file>-meta.yaml`, around 700 bytes,
   containing `file_type` (`GWAS-SSF v1.0` or `pre-GWAS-SSF`),
   `genome_assembly`, `sample_size`, `case_control_study`, `is_harmonised` and
   `data_file_md5sum`. When `file_type` is `GWAS-SSF v1.0` the column set is
   fixed by the standard, so PRS readiness can be established **without touching
   the data file**. This is GWASPoker v2's "API sufficient" branch, and it is a
   stronger result than the deprecated association endpoint could have given.

3. **REST API v2 is published but was unstable during the audit.**
   `GET /gwas/rest/api/v2/studies/{accession}` returned HTTP 500 on every
   attempt, while `/v2/metadata` and `/v2/publications` returned 200 and v1 was
   fully healthy. `catalog/rest_api.py` therefore tries v2 first (it alone
   exposes `full_summary_stats`) and falls back to v1, recording `api_source` in
   provenance so the manuscript can report which route produced each field.

---

## 8. Behaviour deliberately not carried over

* **HuggingChat code generation** (4.14) — replaced by local deterministic
  emission.
* **The `UKB_FIELD` column** (`check_parentheses_and_keywords`) — matched
  `'ukb'` and `'field'` inside the *lower-cased reported trait*; in the shipped
  `Output-Module1-migraine.csv` every row is `-`. No observable behaviour.
* **`sklearn` TF-IDF / KMeans / PCA imports** — imported in Module 1 and the
  notebook, never called.
* **`Information.csv`** — superseded by the structured `FileCandidate` list in
  `probe_results.json`, which records size, type and selection reason.
* **`gwas.csv` as a fixed intermediate filename** — v2 preserves the original
  filename and never overwrites the user's data.
* **`detect_delimiter` package dependency** — replaced by a scoring-based
  delimiter detector that works on in-memory bytes rather than a file path.
