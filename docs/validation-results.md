# Validation Results

Four experiments, run against 50 phenotype queries on the NHGRI-EBI GWAS
Catalog. Full methods, per-phenotype composition, field-level tables and failure
taxonomies are in Supplementary Material 1; the summary is below.

!!! note "Validation software version"

    All manuscript validation experiments were performed using GWASPoker v2.1.0. The frozen cohort definitions, analysis scripts, task-level outputs and derived tables correspond to the manuscript validation run.

## Source pool

| | |
| --- | --- |
| Phenotype queries | 50 |
| Unique GCST accessions after deduplication | 2,208 |
| Summary-statistics file resolved | 2,194 (99.37%) |
| GWAS-SSF metadata sidecar available | 2,114 (95.74%) |
| Harmonised product available | 1,843 (83.47%) |
| Declared GWAS-SSF / pre-SSF / non-SSF / unknown | 1,216 / 673 / 169 / 150 |
| Metadata indicates a raw probe is required | 978 (44.29%) |
| Median resolved file size | 484.50 MB |
| Aggregate size of resolved unique files | ~1.38 TB |

## Dataset 1 — declared GWAS-SSF records

Does GWASPoker recognise the same fields and values as an independent tool?
GWASPoker was **force-probed** so a raw header could be observed even though
metadata alone would have settled readiness. The same source was loaded through
GWASLab with automatic format recognition, and compared before `basic_check`.

| | |
| --- | --- |
| Records analysed | 944 |
| Completed the full paired comparison | 766 (81.14%) |
| **Exact ordered raw-header agreement** | **772 / 772 (100.00%)** |
| Study–field assignments evaluated | 9,638 |
| Directly cross-validatable against GWASLab | 7,897 (81.9361%) |
| **Values compared / identical** | **39,485,000 / 39,479,562 (99.9862%)** |
| Values differing | 5,438 (0.0138%) |

All 5,438 differences are **variant-identifier representation** in three studies
(GCST90302887, GCST90302890, GCST90671940) — rs identifiers against
`chr:pos:ref:alt`. No compared beta, p-value, allele, position, standard error,
INFO, odds ratio or sample-size value differed.

The 81.9361% figure is **comparator coverage, not accuracy**: 954 assignments
concerned concepts GWASLab did not expose in that run and 787 had no
prespecified crosswalk. Neither is evidence of a GWASPoker error. The 178
incomplete records were 169 independent-fetch errors, 6 GWASLab load errors and
3 GWASPoker execution errors.

## Dataset 2 — metadata-uncertain and legacy files

Where structured metadata cannot settle readiness, does GWASPoker reach the same
conclusion as GWASLab? A **tool-neutral raw-header screen ran first**, so
neither tool got credit for information already present in the source.

| | |
| --- | --- |
| Metadata-uncertain studies | 978 |
| Already Core+N-ready in the raw header (excluded) | 712 (72.80%) |
| Not Core+N-ready under neutral source-header screen | 266 |
| Complete paired results | 263 |
| **Core-ready after GWASPoker** | **210 / 263 (79.85%)** |
| **Core-ready after GWASLab** | **210 / 263 (79.85%)** |
| GWASPoker-only / GWASLab-only | 0 / 0 |
| Core+N recovered by either | 0 |
| Paired values compared / identical | 10,820,000 / 10,820,000 (100%) |

The same 210 files, and the same 53 failures. Field recovery was identical for
11 of 14 concepts; GWASLab recovered 2 additional variant IDs, GWASPoker
recovered `Direction` in 30 additional files. Neither restored Core+N, because
the sample-size information required for it was absent from the source —
structural standardisation cannot invent it.

## Dataset 3 — controlled 50-file transfer benchmark

One file per phenotype, selected deterministically (closest to the
phenotype-specific median size, 1.5 GiB cap). **The same file** was then
assessed by bounded probe and by complete transfer plus local GWASLab.

| | |
| --- | --- |
| Files | 50 (all completed) |
| Complete-transfer bytes | 21,189,691,005 |
| Bounded-probe bytes | 13,107,200 |
| **Transfer reduction** | **99.9381%** |
| Full/probe byte ratio | 1,616.65x |
| Summed GWASPoker task time | 1,713.29 s |
| Summed full-transfer + GWASLab time | 11,356.70 s |
| Ratio of summed times | 6.63x |
| Core and Core+N decision agreement | 46 / 49 (93.88%) |

Three files disagreed (GCST90668075, GCST006900, GCST90726617) and one had no
paired comparator decision (GCST90274714), excluded from the 49-file
denominator but retained in the transfer figures.

Timings are **sums of independently executed per-file durations, not parallel
makespan**, and per-file ratios are not uniformly above 1 — a bounded remote
probe is not faster for every server and file.

## Dataset 4 — external heterogeneous URLs

Direct-URL probing against the historical
[`mikegloudemans/gwas-download`](https://github.com/mikegloudemans/gwas-download)
collection: aging URLs, varied hosts, compression conventions and schemas. Not a
census of available GWAS data — a stress test.

| | |
| --- | --- |
| Unique URLs tested / live | 1,791 / 768 (42.88%) |
| Support and document links excluded | 83 |
| Non-support URLs probed | 685 |
| Audited-clean tabular responses | 402 |
| At least one tracked concept recovered | 365 / 402 (90.80%) |
| **Core PRS-ready** | **321 / 402 (79.85%)** |
| Core+N PRS-ready | 228 / 402 (56.72%) |
| Complete-file bytes represented | 66,249,223,088 |
| Observed probe bytes | 105,120,243 |
| Transfer reduction | 99.8413% |

Of the 685 probed non-support URLs, 402 entered the audited-clean structural-mapping denominator. The remaining 283 comprised 281 probe/worker failures and two technically COMPLETE responses excluded by defensive content audit.

The 281 probe/worker failures comprised 194 non-data HTTP responses, 74 HTTP 404s (all 74 EBI-hosted paths in this historical collection), 7 network timeouts, 3 outer task timeouts, 2 unsupported formats and 1 empty response. Transport failures are reported **separately from mapping failures** — a dead link is not a file missing PRS columns. No complete external file was downloaded.

## Reproducing this

The benchmark infrastructure that produced Dataset 3 is in
[`benchmark/`](https://github.com/MuhammadMuneeb007/GWASPokerforPRS2/tree/main/benchmark),
documented under [Benchmarking](benchmarking.md). Full methods, per-phenotype
composition, field-level tables and failure taxonomies are in Supplementary
Material 1.

## Next

- [Benchmarking](benchmarking.md) — the infrastructure, and why ground truth is curated by hand
- [Reproducibility](reproducibility.md) — what to record alongside results
