# Example: probing a remote file

All output below is copied from real runs against `ftp.ebi.ac.uk` on
2026-08-24.

---

## Probing a harmonised file

```bash
gwaspoker probe GCST90038646
```

```text
Study: GCST90038646 — Migraine
File: 33959723-GCST90038646-EFO_0003821.h.tsv.gz
Source: https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/
        GCST90038001-GCST90039000/GCST90038646/harmonised/
        33959723-GCST90038646-EFO_0003821.h.tsv.gz
Selected because: fully harmonised file (.h.tsv); in harmonised/ and harmonised
output was requested; compressed tabular text; filename carries the study
accession; size used only as a tiebreaker (scored 16.43 against 2 other
candidate(s))

Format            TSV.GZIP
Remote size       377.78 MB
Bytes inspected   256.00 KB
Transfer avoided  99.9338%
Range requests    used
Transfer time     2.00 s
Encoding          utf-8 (100% confidence)
Decompressed      959.53 KB (3.7x expansion)

Detected header (row 0, tab-separated, 72% confidence)
  hm_variant_id  hm_rsid  hm_chrom  hm_pos  hm_other_allele  hm_effect_allele
  hm_beta  hm_odds_ratio  hm_ci_lower  hm_ci_upper  hm_effect_allele_frequency
  hm_code  variant_id  chromosome  base_pair_location  effect_allele
  other_allele  effect_allele_frequency  beta  standard_error  p_value
  odds_ratio  ci_lower  ci_upper
```

256 KB of a 377.78 MB file, in two seconds, gives the complete 24-column header.

## The same probe at 64 KB

```bash
gwaspoker probe GCST90038646 --probe-bytes 65536
```

```text
Bytes inspected   64.00 KB
Transfer avoided  99.9835%
Transfer time     0.99 s
Decompressed      235.21 KB (3.7x expansion)
```

The header is identical. 64 KB of gzip inflates to 235 KB of text — hundreds of
rows, where a header needs one. `gwaspoker benchmark --probe-size-sweep` exists
to establish how far down this can safely go across many files; the default of
256 KB is a starting point, not a validated optimum.

---

## Probing the raw file instead

```bash
gwaspoker probe GCST90038646 --harmonised no --probe-bytes 65536
```

```text
File: GCST90038646_buildGRCh37.tsv
Selected because: raw submitted file, as requested; plain tabular text;
filename carries the study accession; size used only as a tiebreaker

Format            TSV
Remote size       1.17 GB
Bytes inspected   64.00 KB
Transfer avoided  99.9948%
Range requests    used
Transfer time     0.98 s
Encoding          utf-8 (100% confidence)

Detected header (row 0, tab-separated, 72% confidence)
  variant_id  chromosome  base_pair_location  GENPOS  effect_allele
  other_allele  effect_allele_frequency  INFO  CHISQ_LINREG  P_LINREG
  beta  standard_error  CHISQ_BOLT_LMM_INF  P_BOLT_LMM_INF  CHISQ_BOLT_LMM
  p_value
```

64 KB of a 1.17 GB file: **99.9948% of the transfer avoided**, in one second.

---

## The mapping table, and a corrected v1 defect

```text
                    Canonical column mapping
  #  Column                   Canonical concept        PRS   Method     Conf.
  0  variant_id               variant_id               SNP   canonical   1.00
  1  chromosome               chromosome               CHR   canonical   1.00
  2  base_pair_location       position                 BP    alias       0.95
  3  GENPOS                   position                 BP    alias       0.95
  4  effect_allele            effect_allele            A1    canonical   1.00
  5  other_allele             other_allele             A2    canonical   1.00
  6  effect_allele_frequency  effect_allele_frequency  EAF   canonical   1.00
  7  INFO                     info_score               INFO  alias       0.95
  8  CHISQ_LINREG             unknown                        unknown        —
  9  P_LINREG                 p_value                  P     alias       0.95
 10  beta                     beta                     BETA  canonical   1.00
 11  standard_error           standard_error           SE    canonical   1.00
 12  CHISQ_BOLT_LMM_INF       unknown                        unknown        —
 13  P_BOLT_LMM_INF           p_value                  P     alias       0.95
 14  CHISQ_BOLT_LMM           unknown                        unknown        —
 15  p_value                  p_value                  P     canonical   1.00

3 column(s) left as unknown rather than forced onto a concept:
CHISQ_LINREG, CHISQ_BOLT_LMM_INF, CHISQ_BOLT_LMM
```

Two things to note.

**`P_BOLT_LMM_INF` maps to `p_value`.** This is the same file the original
implementation was run on — `Input-Module3-Migraine.csv` in the v1 repository
points at exactly this URL — and the v1 output committed alongside it,
`Output-Module4-Migraine-Code.py`, contains:

```python
# BETA -> ['beta', 'p_bolt_lmm_inf']
df_renamed = df.rename(columns={..., 'beta': 'BETA', 'p_bolt_lmm_inf': 'BETA'})
```

`p_bolt_lmm_inf` is the BOLT-LMM infinitesimal-model **p-value**. v1 listed it
in `beta_list`, so it was mapped onto the effect size — and because it comes
later in the rename dictionary, it *overwrote* the real `beta` column. Every
weight in the resulting score would have been a p-value.

**Three chi-squared columns stay `unknown`.** They are real columns with no
canonical concept. GWASPoker names them rather than forcing them onto something
plausible, because a forced mapping propagates silently into the score while an
`unknown` prompts a human to look.

---

## When three columns claim the same concept

Running `assess` on this file raises:

```text
Warnings:
  ! 3 columns map to p_value: P_LINREG, P_BOLT_LMM_INF, p_value. 'p_value' was
    used; confirm that is the intended one rather than 'P_LINREG',
    'P_BOLT_LMM_INF'.
```

BOLT-LMM emits a linear-regression p-value, an infinitesimal-model p-value and
the final mixed-model p-value side by side. Which one belongs in a PRS is an
analyst's decision. GWASPoker uses the highest-confidence mapping — here the
column literally named `p_value` — and names the alternatives rather than
discarding them silently.

---

## Probing a URL directly

```bash
gwaspoker probe https://example.org/study.tsv.gz
```

A bare URL carries no Catalog metadata, so the structured route does not apply
and the file is probed immediately.

---

## How the bound works

1. `HEAD` the URL — learn `Content-Length` and whether `Accept-Ranges: bytes`.
2. If ranges are supported, `GET` with `Range: bytes=0-<N-1>`. The server sends
   exactly `N` bytes and the connection closes.
3. If not, stream the response and close the connection at `N` bytes locally.
4. Detect compression from magic bytes, inflate the prefix incrementally.
5. Detect the encoding, split off the (almost always present) partial last line.
6. Score every line as a header candidate; pick the best; map the columns.

The bound is on **bytes**, which is reproducible and is the quantity reported.
v1 used `timeout -s KILL 10 wget`, so how much data moved depended on the
network that day.

Recorded on every probe:

```json
{
  "requested_bytes": 262144,
  "received_bytes": 262144,
  "remote_file_size": 396130130,
  "range_supported": true,
  "range_used": true,
  "transfer_time_seconds": 2.0021,
  "http_status": 206,
  "request_count": 2,
  "transfer_reduction": 0.999338
}
```

---

## Machine-readable output

```bash
gwaspoker probe GCST90038646 --output probe_results.json
```

`probe_results.json` carries the study, the resolved file **with every rejected
candidate and the reason each scored as it did**, the transfer statistics above,
the decompression record, the header with its confidence and preamble lines, and
the full column mapping — plus the provenance block.

---

## Failure reporting

```bash
gwaspoker probe GCST99999999
```

```text
[invalid_accession] study_metadata: GCST99999999: GCST99999999 is not present
in the GWAS Catalog (confirmed by an HTTP 404 from the metadata API)
```

Note the phrasing: *confirmed by an HTTP 404*. A study is reported absent only
on an explicit 404 from a healthy endpoint. A 500 or a timeout is
`api_error` or `network_timeout`, and the fallback route runs — a single failed
request never becomes a claim about the data.

Every failure carries a category from a fixed vocabulary
(`http_404`, `http_403`, `range_not_supported`, `decompression_error`,
`header_not_found`, `truncated_probe`, `api_deprecated`, ...), and
`--failure-log failures.jsonl` persists them for later analysis.
