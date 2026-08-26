# Benchmark

Infrastructure for evaluating GWASPoker scientifically.

**No results are recorded here.** This directory contains the manifest schema, a
template, and instructions. Metrics belong in the manuscript, computed from real
runs against real labels — not hard-coded into a repository file.

---

## The principle that governs everything here

> Do not use the same parser to construct the ground truth and to evaluate
> itself.

If `ground_truth_header` is filled in by running GWASPoker, then comparing it to
`predicted_header` measures nothing except that the code is deterministic. The
manifest therefore has two clearly separated halves:

| Half | Filled by | Columns |
| --- | --- | --- |
| **Predictions** | `gwaspoker benchmark --run` | `predicted_*`, `api_*`, `probe_*`, `file_*` |
| **Ground truth** | **a human, by hand** | `ground_truth_header_row_index`, `ground_truth_header`, `ground_truth_mapping`, `ground_truth_prs_ready` |

GWASPoker never writes a `ground_truth_*` column. `validate_manifest` also
checks for the signature of accidental circularity: if three or more labelled
rows have `ground_truth_header` byte-identical to `predicted_header`, the report
says so rather than emitting a flattering 100%.

```text
! All 12 labelled rows have ground_truth_header identical to predicted_header.
  If the labels were produced by GWASPoker itself the evaluation is circular and
  its metrics are meaningless. Curate the ground truth independently.
```

---

## Manifest schema

28 columns. `benchmark_manifest_template.csv` ships with all of them and three
worked example rows.

### Study identity

| Column | Notes |
| --- | --- |
| `study_accession` | GCST accession |
| `trait` | Reported trait |
| `source` | `GWAS Catalog`, or another repository |
| `publication_year` | For pre/post-GWAS-SSF stratification |
| `ssf_status` | `GWAS-SSF` / `pre-GWAS-SSF` / blank; auto-filled from the sidecar |

### Structured-route measurements

`summary_statistics_api_available`, `api_sufficient`, `api_bytes`, `api_latency`

### File and probe measurements

`remote_file_url`, `file_format`, `compression`, `full_file_size`,
`probe_bytes`, `probe_latency`

### Predictions

`predicted_header_row_index`, `predicted_header`, `predicted_delimiter`,
`predicted_mapping`, `predicted_prs_ready`

### Ground truth — curated by hand

`ground_truth_header_row_index`, `ground_truth_header`, `ground_truth_mapping`,
`ground_truth_prs_ready`

### Integration and notes

`gwaslab_detection`, `gwaslab_success`, `failure_category`, `notes`

### Cell formats

* **Headers** — tab-separated within the cell, so a column name containing a
  comma survives. Order is preserved and is scored.
* **Mappings** — `raw=canonical|raw=canonical|...`
* **Verdicts** — `READY`, `PARTIAL`, `NOT_READY`, `UNKNOWN`.
  `yes`/`true`/`1` and `no`/`false`/`0` are also accepted.
* **Empty** means unknown, and is skipped rather than counted as a miss.

---

## Workflow

### 1. Build the study list

Pick studies to span the strata you want to report on: GWAS-SSF and
pre-GWAS-SSF, compressed and uncompressed, several formats, a range of years and
sizes, and — importantly — some that are expected to fail.

```bash
gwaspoker search --trait migraine --sumstats-only --limit 50 \
    --output migraine.csv --format csv
```

Then build a manifest with `study_accession` and `trait` filled in. Everything
else can be blank.

### 2. Curate the ground truth — before running GWASPoker

Do this first, so the labels cannot be influenced by GWASPoker's output.

For each study, independently establish:

* **`ground_truth_header_row_index`** — the 0-based index of the header line
  among the file's lines.
* **`ground_truth_header`** — the header, tab-separated, **in file order**.
* **`ground_truth_mapping`** — what each column actually means. Read the
  publication, the README, or the GWAS-SSF metadata; do not read GWASPoker's
  output.
* **`ground_truth_prs_ready`** — your own verdict against the rules in
  [`../docs/mapping.md`](../docs/mapping.md).

Practical ways to get an independent header:

```bash
# The header, without GWASPoker: first line of the decompressed stream.
curl -sL "<url>" | gzip -dc 2>/dev/null | head -n 5

# Or, on a downloaded file:
zcat file.tsv.gz | head -n 5        # Linux/macOS
python -c "import gzip,sys; print(gzip.open(sys.argv[1],'rt').readline())" file.tsv.gz
```

Record in `notes` how each label was established and when. That is what makes
the evaluation auditable.

### 3. Run GWASPoker

```bash
gwaspoker benchmark manifest.csv --run --update-manifest manifest_filled.csv
```

Fills the prediction and measurement columns. `--run` implies `--force-probe`,
so both routes are measured on every study and the API-sufficient branch can be
compared against the probe on the same files.

Options: `--probe-bytes`, `--harmonised`.

### 4. Score

```bash
gwaspoker benchmark manifest_filled.csv --output metrics.json
gwaspoker benchmark manifest_filled.csv \
    --stratify ssf_status,api_coverage,compression,file_format \
    --output metrics.json
```

---

## Metrics computed

### Header detection

| Metric | Definition |
| --- | --- |
| `header_row_accuracy` | Fraction of files whose header *row index* was correct |
| `exact_ordered_header_match` | Fraction whose full header matched exactly, **in order** |
| `exact_ordered_header_match_case_sensitive` | The same, without case folding |
| `header_detected_rate` | Fraction where any header was found |

Comparison is tuple-to-tuple. Never `set` — order is part of the header's
meaning, and v1's set comparison destroyed it.

### Column level

Micro-averaged `precision`, `recall`, `f1` over all columns of all files, using
**multiset** semantics: a header with two columns named `P` against a truth with
one is a false positive, which set arithmetic would hide.

### Canonical mapping

Overall `accuracy`, `unknown_rate`, and a `per_concept` breakdown so a concept
that is systematically mis-mapped is visible rather than averaged away.

### PRS readiness

Two confusion matrices — strict (`READY` positive) and lenient
(`READY` or `PARTIAL` positive) — each giving `sensitivity`, `specificity`,
`precision`, `recall`, `f1`, `accuracy`, `false_positive_rate`,
`false_negative_rate`. Plus full multiclass agreement with a confusion table.

**The false positive is the expensive error**: GWASPoker said a file was usable,
the user downloaded several gigabytes, and it was not. Report it prominently.

### Transfer and latency

`total_bytes_transferred`, `total_full_file_bytes`,
`percentage_transfer_reduction`, mean and median probe bytes, mean API bytes,
mean and median probe latency, mean API latency.

### Coverage and failure

`api_coverage`, `api_sufficiency_rate`, `failure_rate`, and a count per
`failure_category`.

### Undefined is not zero

Every metric returns `null` when there were no cases to compute from. "No
positive cases, so recall is undefined" and "recall is zero" are different
scientific statements and are reported differently.

---

## Stratification

```bash
--stratify ssf_status,api_coverage,file_format,compression,source
```

| Key | Buckets |
| --- | --- |
| `ssf_status` | `GWAS-SSF` vs `pre-GWAS-SSF` |
| `api_coverage` | `api_covered` vs `api_uncovered` |
| `file_format` | `TSV`, `TSV.GZIP`, `CSV`, ... |
| `compression` | `none`, `gzip`, `zip`, `bgzf`, ... |
| `source` | Repository |

Each stratum reports its own header detection, PRS readiness, transfer and
coverage figures.

---

## The probe-size experiment

> How many bytes are needed for reliable header detection?

256 KB is GWASPoker's default. **It is a starting point, not a validated
optimum.** This experiment is how the right number gets established.

```bash
gwaspoker benchmark manifest.csv --probe-size-sweep --output sweep.json
```

Probes every study at 64 KB, 128 KB, 256 KB, 512 KB and 1 MB, recording for each
whether a header was detected, what it was, the confidence, the bytes actually
received and the latency.

```text
                    Probe-size experiment
 Probe size  Header detected  Agrees with largest probe  Mean bytes received
     65,536       (n/n)              (n/n)                     ...
    131,072       (n/n)              (n/n)                     ...
    262,144       (n/n)              (n/n)                     ...
    524,288       (n/n)              (n/n)                     ...
  1,048,576       (n/n)              (n/n)                     ...
```

**A caveat the summary states in its own output:** agreement is measured against
each file's own largest successful probe, not against curated ground truth. That
is a self-consistency check, useful for spotting where a small probe changes the
answer, but it is not an accuracy figure. For accuracy, use the manifest's
`ground_truth_header` column and re-run the scoring at each probe size:

```bash
for size in 65536 131072 262144 524288 1048576; do
  gwaspoker benchmark manifest.csv --run --probe-bytes "$size" \
      --update-manifest "filled_${size}.csv"
  gwaspoker benchmark "filled_${size}.csv" --output "metrics_${size}.json"
done
```

That gives a genuine accuracy-versus-bytes curve.

---

## Pilot comparison table

The table intended to become the basis of the scientific evaluation is produced
by `gwaspoker run` and by `assess` over multiple accessions:

```text
GCST · Trait · SSF/API status · API available · API sufficient · API bytes ·
Raw file size · Probe bytes · Header detected · PRS ready ·
Full download required · GWASLab success
```

```bash
gwaspoker assess GCST1 GCST2 GCST3 --output pilot.csv --format csv
gwaspoker run --trait migraine --target prs --output pilot.html --format html
```

`prs_assessment.csv` carries all of those columns plus the provenance needed to
reproduce each row.

---

## Reproducibility

Every run should be recorded with provenance:

```bash
gwaspoker benchmark manifest.csv --run \
    --provenance provenance.json \
    --failure-log failures.jsonl
```

The provenance record fixes the GWASPoker version, Python version, platform,
timestamp, GWAS Catalog data release, EFO version and the full configuration.
Since the Catalog changes continuously, a benchmark result is only meaningful
alongside the release it was computed against.

---

## Suggested reporting

For the manuscript, report at minimum:

1. **n**, and how the studies were selected.
2. **Ground-truth provenance** — how each label was established, by whom, when.
   Include the `notes` column.
3. **Header detection** — exact ordered match, with a confidence interval.
4. **PRS readiness** — the strict confusion matrix, with the false-positive rate
   given its own emphasis.
5. **Transfer reduction** — median and range, not just the mean; file sizes vary
   over three orders of magnitude.
6. **Failures** — the rate and the category breakdown. A tool that silently
   fails on 20% of files is not a 100%-accurate tool.
7. **Stratified results** — GWAS-SSF versus pre-GWAS-SSF especially, since the
   two take entirely different routes through the software.
8. **The probe-size curve**, and the value it justifies.

State clearly which numbers came from the structured route (0 data bytes) and
which from the probe. They are different measurements of different things.
