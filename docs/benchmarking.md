# Benchmarking

```bash
gwaspoker benchmark manifest.csv --run --output results/
```

The full manifest schema and curation instructions live in
[`benchmark/README.md`](https://github.com/MuhammadMuneeb007/GWASPokerforPRS2/blob/main/benchmark/README.md).
This page covers the design.

---

## The principle that governs everything here

!!! danger "Do not use the same parser to construct the ground truth and to evaluate itself"

    If `ground_truth_header` is filled in by running GWASPoker, comparing it to
    `predicted_header` measures nothing except that the code is deterministic.

The manifest has two clearly separated halves:

| Half | Filled by | Columns |
| --- | --- | --- |
| **Predictions** | `gwaspoker benchmark --run` | `predicted_*`, `api_*`, `probe_*`, `file_*` |
| **Ground truth** | **a human, by hand** | `ground_truth_header_row_index`, `ground_truth_header`, `ground_truth_mapping`, `ground_truth_prs_ready` |

**GWASPoker never writes a `ground_truth_*` column.**

### Circularity is detected, not assumed away

`validate_manifest` looks for the signature of accidental circularity. If three
or more labelled rows have `ground_truth_header` byte-identical to
`predicted_header`, the report says so rather than emitting a flattering 100%:

```text
! All 12 labelled rows have ground_truth_header identical to predicted_header.
  If the labels were produced by GWASPoker itself the evaluation is circular and
  its metrics are meaningless. Curate the ground truth independently.
```

### Why GWASLab is never called during analysis

GWASLab serves as one external ground-truth source. That is precisely why it is
**never** called from probing, mapping, value validation or readiness
assessment — a predictor that consults its own grader is not being graded.

The only GWASLab integration is the optional post-download hand-off in
[`download`](download-extract.md#handing-off-to-gwaslab).

---

## No results are stored in the repository

The `benchmark/` directory contains the manifest schema, a template, and
instructions. **It contains no metrics.** Numbers belong in the manuscript,
computed from real runs against real labels — not hard-coded into a repository
file where they cannot be audited or reproduced.

---

## What is measured

| Metric | Question |
| --- | --- |
| **Header row accuracy** | Was the header found on the right line? |
| **Header content accuracy** | Are the column names exactly right? |
| **Mapping accuracy** | Did each column reach the right canonical concept? |
| **Readiness agreement** | Does the verdict match the curated one? |
| **Bytes transferred** | How much was avoided against the full file size? |
| **Latency** | Wall-clock time to a verdict. |

### Stratification

```bash
gwaspoker benchmark manifest.csv --run \
    --stratify ssf_status,file_format,compression
```

Available strata: `ssf_status`, `api_coverage`, `file_format`, `compression`,
`source`. Aggregate accuracy hides the interesting cases — a tool can score well
overall while failing every archive, and stratifying is how that becomes visible.

### Probe-size sweep

```bash
gwaspoker benchmark manifest.csv --probe-size-sweep
```

Probes each study at 64 KB, 128 KB, 256 KB, 512 KB and 1 MB, so the accuracy
gained per byte can be measured rather than assumed. This is what the 256 KB
default is justified against.

---

## Writing predictions back

```bash
gwaspoker benchmark manifest.csv --run --update-manifest filled.csv
```

Writes the manifest back with the prediction columns filled in. The
`ground_truth_*` columns are passed through untouched — the writer refuses to
populate them.

---

## Reporting results honestly

For a paper, report alongside the metrics:

- **The GWASPoker version** and the exact commit.
- **The GWAS Catalog data release and EFO version** at run time — both are in
  the provenance file.
- **`probe_bytes`**, since accuracy is a function of it.
- **How ground truth was curated**, and by whom.
- **The failure log**, so excluded rows are visible rather than silently absent.
- **Which route produced each verdict** (`readiness_evidence_source`), since
  the metadata route and the probe route have different cost profiles.

See [Reproducibility](reproducibility.md).

---

## Next

- [Reproducibility](reproducibility.md) — what provenance captures
- [CLI Reference](cli-reference.md#benchmark) — every flag
