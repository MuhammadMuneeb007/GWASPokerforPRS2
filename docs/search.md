# Find Studies

```bash
gwaspoker search --trait "coronary artery disease" --sumstats-only --limit 20
```

`search` answers a narrower question than it looks: *which studies for this
phenotype have files worth looking at?* It does not read any data.

---

## The output columns

```text
 GCST          Trait                    File  SSF Meta  Harmonised  GWAS-SSF  PRS  Probe
 GCST90132314  Coronary artery disease  yes   yes       yes         yes       yes  no
 GCST005194    Coronary artery disease  yes   no        yes         ?         ?    yes
 GCST011364    Coronary artery disease  no    ?         ?           ?         ?    ?
```

| Column | Means |
| --- | --- |
| **File** | A summary-statistics file exists at the expected FTP path. |
| **SSF Meta** | A `-meta.yaml` sidecar exists beside it. |
| **Harmonised** | A harmonised (`/harmonised/`, `.h.tsv.gz`) version is published. |
| **GWAS-SSF** | The sidecar declares `file_type: GWAS-SSF v1.0`. |
| **PRS** | Readiness can be decided from that declaration alone. |
| **Probe** | Whether reading bytes would be required to be sure. |

!!! warning "`?` means unchecked, not absent"

    This distinction is load-bearing. `no` is a positive finding: the file is
    not there. `?` means GWASPoker did not look — because `--no-check-files`
    was passed, or because an earlier column already answered the question.
    Treating the two as the same would silently convert *unknown* into
    *missing*.

---

## Two stages, and why the second is optional

The first stage is a single GWAS Catalog query. The second checks each result's
FTP directory and sidecar — **two to three requests per study**.

```bash
# Fast: metadata only, file columns show `?`
gwaspoker search --trait migraine --no-check-files

# Complete: file columns resolved
gwaspoker search --trait migraine --check-files --workers 8
```

Stage two is parallelised. Measured on 300 studies: **22.8 s at one worker,
9.7 s at six** — a 2.35× speed-up that flattens beyond about ten workers,
because all threads share one process-wide rate limiter and the request rate,
not the thread count, becomes the floor.

!!! info "Threads do not raise the request rate"

    `--workers` overlaps latency; it does not send more requests per second.
    The limiter is deliberately conservative for `ftp.ebi.ac.uk`, which
    publishes no documented rate limit — the 15 queries/second figure in the
    GWAS Catalog documentation applies to REST API v2, a different service.

---

## Narrowing results

=== "By ancestry"

    ```bash
    gwaspoker search --trait asthma --population European
    ```

=== "Only studies with files"

    ```bash
    gwaspoker search --trait asthma --sumstats-only
    ```

=== "Excluding trait text"

    ```bash
    gwaspoker search --trait asthma \
        --exclude "childhood" --exclude "occupational"
    ```

    Repeatable, case-insensitive, matched against the reported trait.

=== "Keeping gene-based burden results"

    ```bash
    gwaspoker search --trait asthma --no-default-excludes
    ```

    By default GWASPoker drops studies whose trait contains *Gene-based
    burden*. Those are not variant-level summary statistics and cannot yield
    PRS weights, so including them would inflate the candidate count with rows
    that can never be `READY`. `--no-default-excludes` keeps them.

---

## Output formats

```bash
gwaspoker search --trait migraine --format json --output studies.json
gwaspoker search --trait migraine --format csv > studies.csv
gwaspoker search --trait migraine --format html --output studies.html
```

`--format` is honoured with or without `--output`: `json` and `csv` go to
stdout when no path is given, so they can be piped.

---

## Where the sample counts come from

`--show-provenance` prints the source of every sample count, in the order
GWASPoker tried:

1. **The GWAS Catalog API** — a structured `discoverySampleAncestries` entry.
2. **A regex over the reported free-text sample description** — used only when
   the API has no structured count.
3. **The ELECTRA model**, if `--llm` is passed and the extra is installed.

The LLM route is **off by default**. A sample size that came from a language
model is labelled as such in every report, so it is never mistaken for a
curated figure.

---

## Next

- [Assess for PRS](assess.md) — turn a candidate into a verdict
- [CLI Reference](cli-reference.md#search) — every `search` flag
