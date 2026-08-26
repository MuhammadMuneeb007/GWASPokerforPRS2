# Reproducibility

A verdict that cannot be reproduced is an opinion. Every GWASPoker run can emit
a provenance file that records exactly what produced it.

```bash
gwaspoker assess GCST90271311 --provenance run.json
```

---

## What provenance captures

### Environment

```json
{
  "gwaspoker_version": "2.1.0",
  "python_version": "3.13.2",
  "platform": "Windows-11-10.0.26200-SP0",
  "timestamp_utc": "2026-08-26T05:36:01+00:00",
  "command": "gwaspoker assess GCST90271311",
  "catalog_data_release": "2026-08-22",
  "catalog_api_version": "2.0",
  "efo_version": "v3.93.0"
}
```

!!! important "The data release matters as much as the tool version"

    The GWAS Catalog is a moving target. A study that returns `READY` today may
    return `NOT_READY` after a re-harmonisation, and vice versa. Reporting
    `catalog_data_release` and `efo_version` is what makes a result checkable
    later — the GWASPoker version alone is not enough.

### Configuration

The **fully resolved** configuration — every value after defaults, config file,
environment variables and CLI flags have all been applied. This is what should
be reported, not the config file you wrote, because the file is only one of four
layers.

### Operations

Per operation: which endpoint answered, which file was selected and **why**,
bytes moved, latency, detected encoding, delimiter, header row, the complete
column mapping with methods and confidences, value-validation results, and the
verdict.

### Failures

Every classified failure encountered, with its category. A run that partially
failed says so.

---

## For a manuscript

Report these five things:

| Report | Why |
| --- | --- |
| **GWASPoker version and commit** | `2.1.0` plus the exact revision. |
| **GWAS Catalog data release and EFO version** | Both in `environment`. |
| **`probe_bytes`** | Accuracy is a function of it; a result at 64 KB is not a result at 1 MB. |
| **`readiness_evidence_source` per study** | The metadata route and the probe route have different cost profiles and should be reported separately. |
| **The failure log** | So excluded studies are visible rather than silently absent. |

```bash
gwaspoker run --trait migraine --limit 100 \
    --provenance provenance.json \
    --failure-log failures.jsonl \
    --format json --output results.json
```

---

## Determinism

GWASPoker is deterministic given the same inputs and the same upstream state.

- **No randomness** in header detection, mapping or readiness.
- **No time-dependent behaviour** beyond timeouts and rate limiting.
- **Thread count does not change results.** `--workers` overlaps latency; each
  study is resolved independently, and the output is ordered by the search
  result, not by completion order.

What is *not* deterministic across runs is the upstream: the Catalog's contents,
FTP availability, and network conditions. That is why the data release is
recorded.

---

## Pinning the environment

```bash
pip install -e .
pip freeze > environment-lock.txt
```

Or use the shipped `environment.yml`:

```bash
conda env create -f environment.yml
conda activate gwaspoker
```

Runtime dependencies are deliberately few — `requests`, `typer`, `rich`,
`pyyaml`, `charset-normalizer`. Everything heavier is an optional extra that the
core never imports.

---

## Verifying an install

```bash
pip install -e ".[dev]"
pytest -q                 # 665 unit tests, no network
pytest -q -m integration  # 15 tests against the live GWAS Catalog
```

The integration tests assert the **contract** GWASPoker depends on — that Range
requests are honoured, that the GWAS-SSF sidecar parses, that the withdrawn API
still answers 410 — not values the Catalog is free to change. They exist to
detect upstream drift, and a failure there is information about the Catalog, not
necessarily a bug in GWASPoker.

CI runs the unit suite on Python 3.9 and 3.13, on Linux and Windows, on every
push.

---

## Next

- [Configuration](configuration.md) — every setting provenance records
- [Benchmarking](benchmarking.md) — evaluating against curated ground truth
