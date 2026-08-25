# Example: assessing PRS readiness

All output below is copied from real runs on 2026-08-24.

`assess` is the command that makes the decision. It tries the structured route
first and probes the file only when that route cannot settle the question.

---

## Case 1 — the structured route suffices (0 data bytes)

```bash
gwaspoker assess GCST90271311 --target prs --harmonised no
```

```text
Study: GCST90271311
Trait: Platelet count
Source: GWAS Catalog (gwas_catalog_rest_v1)
API assessment: sufficient — no data bytes needed
Declared format: GWAS-SSF v1.0 (genome build GRCh38, harmonised=False)
Remote file: GCST90271311.tsv.gz (raw)
Remote size: 316.00 MB
Bytes inspected: 0 — raw file probing was unnecessary

PRS readiness: READY
Evidence: gwas_ssf_metadata (GWAS-SSF v1.0)

Required fields:
  ✓ variant identification <- chromosome, base_pair_location
  ✓ effect allele <- effect_allele
  ✓ other allele <- other_allele
  ✓ effect size <- beta
  ✓ p-value <- p_value

Recommended:
  ✓ standard error <- standard_error
  ✗ sample size (per-variant N; some methods accept a study-level N instead)
  ✓ allele frequency <- effect_allele_frequency
  ✗ imputation quality (INFO) (lets poorly imputed variants be excluded)

Decision:
  Suitable for downstream PRS preparation. Note that sample size, imputation
  quality (INFO) are absent, which rules out methods that need them.
  Assessed from the file's declared GWAS-SSF conformance; the data file was
  not read.
Note: Raw file probing was unnecessary: the structured metadata was sufficient.
```

**A verdict on a 316 MB file having transferred 1,206 bytes.**

That figure is `total_bytes_transferred` from the provenance record for this
exact run: the GWAS-SSF sidecar plus the deprecated API's response body. The
study-metadata and directory-listing requests add a few kilobytes more; the
*data file* contributes nothing.

This works because the file's `-meta.yaml` sidecar declares
`file_type: GWAS-SSF v1.0`, and that standard fixes the mandatory column set:

```text
chromosome  base_pair_location  effect_allele  other_allele
<beta|odds_ratio|hazard_ratio|z-score>  standard_error
effect_allele_frequency  p_value
```

Which satisfies every required field. No inference about the file's contents is
being made — the file *declares* its conformance.

---

## Case 2 — the structured route is insufficient, so the file is probed

```bash
gwaspoker assess GCST90038646 --target prs
```

```text
Study: GCST90038646
Trait: Migraine
Source: GWAS Catalog (gwas_catalog_rest_v1)
API assessment: insufficient (metadata present but the column set is not guaranteed)
Declared format: pre-GWAS-SSF (genome build GRCh38, harmonised=True)
Remote file: 33959723-GCST90038646-EFO_0003821.h.tsv.gz (harmonised)
Remote size: 378.00 MB
Bytes inspected: 256.00 KB

Detected header:
  hm_variant_id  hm_rsid  hm_chrom  hm_pos  hm_other_allele  hm_effect_allele
  hm_beta  hm_odds_ratio  hm_ci_lower  hm_ci_upper  hm_effect_allele_frequency
  hm_code  variant_id  chromosome  base_pair_location  effect_allele
  other_allele  effect_allele_frequency  beta  standard_error  p_value
  odds_ratio  ci_lower  ci_upper

PRS readiness: READY
Evidence: file_probe

Required fields:
  ✓ variant identification <- variant_id
  ✓ effect allele <- effect_allele
  ✓ other allele <- other_allele
  ✓ effect size <- beta
  ✓ p-value <- p_value
```

`file_type: pre-GWAS-SSF` guarantees nothing about the columns, so the probe
ran. `Evidence: file_probe` records that the verdict came from the observed
header rather than a declaration.

---

## Case 3 — comparing the two routes

This is the measurement the benchmark is built around.

```bash
# structured route only (the default)
gwaspoker assess GCST90271311 --target prs --harmonised no

# force the probe as well
gwaspoker assess GCST90271311 --target prs --harmonised no --force-probe
```

Forced-probe run:

```text
API assessment: sufficient — no data bytes needed
Declared format: GWAS-SSF v1.0 (genome build GRCh38, harmonised=False)
Bytes inspected: 256.00 KB

Detected header:
  chromosome  base_pair_location  effect_allele  other_allele  beta
  standard_error  effect_allele_frequency  p_value

PRS readiness: READY
Evidence: file_probe
```

| | API-only | Forced probe |
| --- | --- | --- |
| Data bytes transferred | 0 | 262,144 |
| Verdict | `READY` | `READY` |
| Evidence | `gwas_ssf_metadata (GWAS-SSF v1.0)` | `file_probe` |

**The observed header is exactly the GWAS-SSF v1.0 mandatory set, in the
standard's order.** The declaration and the file agree.

That agreement is what licenses skipping the probe on SSF-conformant files. It
is also a claim that must be tested at scale rather than assumed, which is what
`gwaspoker benchmark --run` does across a manifest. When the two disagree,
GWASPoker keeps the probed result (stronger evidence) and says so:

```text
Note: Structured metadata implied READY but the probed header gives PARTIAL.
```

---

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `READY` | Every required field identified confidently |
| `PARTIAL` | Required fields present but uncertain, or one to two missing |
| `NOT_READY` | Three or more required fields could not be identified |
| `UNKNOWN` | Nothing could be evaluated |

Required: variant identification (rsID, or chromosome **and** position); effect
allele; other allele; an effect size (beta, OR, HR or z-score); a p-value.

Recommended: standard error; sample size; allele frequency; INFO. Their absence
never changes the verdict — it changes which downstream methods are available,
and the decision text says which.

Full rules: [`docs/MAPPING_SCHEMA.md`](../docs/MAPPING_SCHEMA.md).

---

## Warnings

A `READY` verdict does not mean "use these numbers as they are". Real example
from `GCST90038646_buildGRCh37.tsv`:

```text
Warnings:
  ! 3 columns map to p_value: P_LINREG, P_BOLT_LMM_INF, p_value. 'p_value' was
    used; confirm that is the intended one rather than 'P_LINREG',
    'P_BOLT_LMM_INF'.
```

Others GWASPoker raises:

| Situation | Warning |
| --- | --- |
| Odds ratios, no beta | Take the natural log before using as weights |
| Hazard ratios, no beta | Take the natural log before using as weights |
| Z-score is the only effect measure | Conversion also needs EAF and per-variant N |
| −log₁₀(p) present, p absent | Transform before applying a p-value threshold |
| MAF present, EAF absent | Which allele is minor is population-dependent |
| Combined `chr:pos` field | Must be split before most tools can read it |
| Any heuristic mapping | The columns concerned are named |

None of these transformations is applied automatically. Log-transforming an odds
ratio is the analyst's decision, and the downstream tool's job.

---

## Multiple studies at once

```bash
gwaspoker assess GCST90271641 GCST90043745 GCST90475837 \
    --target prs --output prs_assessment.csv --format csv
```

```text
                         Assessment summary
 GCST          Trait                   SSF status    API suff.  File size     Probed   PRS         N
 GCST90271641  Migraine                GWAS-SSF         yes      73.00 MB        0 B  READY  513,266
 GCST90043745  Migraine (PheCode 340)  pre-GWAS-SSF     no      507.00 MB  256.00 KB  READY  456,348
 GCST90475837  Migraine (PheCode 340)  GWAS-SSF         yes       1.00 GB        0 B  READY  437,667
```

Two of the three declared GWAS-SSF conformance and needed no data bytes at all.
The third is `pre-GWAS-SSF`, so its header was probed at 256 KB. All three are
`READY` — but only one of them cost a transfer.

`prs_assessment.csv` carries one row per study with the full record: route,
bytes, latency, file selected and why, format, compression, encoding, delimiter,
header row index, the header itself, header confidence, verdict, confidence,
evidence source, missing required fields, unmapped columns, total bytes and any
failure category.

---

## Provenance

```bash
gwaspoker assess GCST90038646 --provenance provenance.json
```

```json
{
  "environment": {
    "gwaspoker_version": "2.0.0",
    "python_version": "3.13.2",
    "platform": "Windows-11-10.0.26200-SP0",
    "timestamp_utc": "2026-08-24T15:47:22+00:00",
    "command": "gwaspoker assess GCST90038646",
    "catalog_data_release": "2026-08-22",
    "catalog_api_version": "2.0",
    "efo_version": "v3.93.0"
  },
  "configuration": { "probe_bytes": 262144, "prefer_harmonised": "auto" },
  "operations": [
    {
      "operation": "assess",
      "study_accession": "GCST90038646",
      "api_source": "gwas_catalog_rest_v1",
      "ssf_status": "pre-GWAS-SSF",
      "api_sufficient": false,
      "api_bytes": 908,
      "remote_file_size": 396130130,
      "file_selection_reason": "fully harmonised file (.h.tsv); ...",
      "probe_bytes_transferred": 262144,
      "probe_range_used": true,
      "probe_transfer_reduction": 0.999338,
      "detected_delimiter": "\t",
      "detected_header_row_index": 0,
      "detected_header": ["hm_variant_id", "hm_rsid", "..."],
      "header_confidence": 0.7225,
      "canonical_mappings": { "hm_chrom": "chromosome", "...": "..." },
      "prs_verdict": "READY",
      "readiness_evidence_source": "file_probe",
      "total_bytes_transferred": 263052
    }
  ]
}
```

Enough to reproduce the experiment: the Catalog release, the exact file, the
probe limit, the bytes that actually moved, and every intermediate decision.

---

## End to end

```bash
gwaspoker run --trait migraine --population European --target prs \
    --output gwaspoker_report.html --format html
```

Searches, assesses every candidate, and writes a ranked comparison plus a
self-contained HTML report. **Nothing is downloaded** unless `--download` is
given.

```text
                          Ranked candidates for migraine
 GCST          Trait                   SSF status    API suff.  File size     Probed   PRS         N
 GCST90271641  Migraine                GWAS-SSF         yes      73.00 MB        0 B  READY  513,266
 GCST90043745  Migraine (PheCode 340)  pre-GWAS-SSF     no      507.00 MB  256.00 KB  READY  456,348
 GCST90079826  ICD10 G43.9: Migraine,  pre-GWAS-SSF     no       19.00 MB  256.00 KB  READY  387,898
               unspecified
 GCST90081731  Migraine (Gene-based    pre-GWAS-SSF     no        4.30 MB  256.00 KB  READY  331,754
               burden)

Ranked by: PRS verdict, then whether the structured route sufficed, then sample
size, then publication year. Every ranking input is a column above.

Transferred 772.60 KB to triage 603.30 MB of published summary statistics
(99.8749% avoided).
```

Four candidates triaged for the cost of 772 KB. One needed no data bytes
because it declares GWAS-SSF v1.0; the other three were probed at 256 KB each.

The ranking is deliberately transparent: each input is a visible column, so you
can re-sort on whichever criterion matters to you. There is no opaque
"best study" score.
