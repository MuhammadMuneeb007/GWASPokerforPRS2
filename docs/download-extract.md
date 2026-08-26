# Download and Extract

Once a study is worth having, two commands fetch it and turn it into a table
downstream tools can read.

---

## `download`

```bash
gwaspoker download GCST90271311 --output-dir ./sumstats
```

Transfers the complete file and verifies it against the published MD5.

| Flag | Effect |
| --- | --- |
| `--output-dir`, `-d` | Directory to write into. |
| `--harmonised auto\|yes\|no` | Which variant to fetch. `auto` prefers harmonised. |
| `--overwrite` | Replace an existing file instead of refusing. |
| `--no-verify` | Skip MD5 verification. |
| `--gwaslab` | Hand the file to GWASLab afterwards. |

!!! warning "`--no-verify` is a last resort"

    Checksum verification is the only thing standing between a truncated
    transfer and a silently wrong PRS. Use `--no-verify` only when the source
    publishes no checksum at all.

### Resuming

An interrupted transfer resumes from where it stopped when the server supports
range requests. GWASPoker re-verifies the completed file against the published
checksum either way, so a resumed download is not trusted more than a fresh one.

---

## `extract`

```bash
gwaspoker extract ./sumstats/GCST90271311.tsv.gz -o clean.tsv --rename
```

Decompresses and writes a clean table.

!!! danger "Only declared transformations"

    GWASPoker **never rewrites data values to make a parser succeed**. Every
    transformation applied is named in the report. A file that needs repairing
    is reported as needing repair — it is not quietly repaired.

### Renaming columns

Two vocabularies, for two audiences:

=== "`--rename` (canonical)"

    ```bash
    gwaspoker extract in.tsv.gz -o out.tsv --rename
    ```

    Columns take their canonical concept names: `variant_id`, `effect_allele`,
    `other_allele`, `beta`, `standard_error`, `p_value`, `effect_allele_frequency`.

=== "`--rename-symbols` (tool symbols)"

    ```bash
    gwaspoker extract in.tsv.gz -o out.tsv --rename --rename-symbols
    ```

    Columns take the short forms PRS tools expect: `CHR`, `BP`, `SNP`, `A1`,
    `A2`, `BETA`, `SE`, `P`, `N`.

An unmapped column keeps its original name. It is never dropped and never
guessed at.

### Other options

| Flag | Effect |
| --- | --- |
| `--delimiter` | Output delimiter. Default is tab. |
| `--max-rows` | Read at most this many rows — useful for a sample. |
| `--overwrite` | Replace an existing output file. |
| `--report PATH` | Write the transformation report as JSON. |

```bash
gwaspoker extract in.tsv.gz -o sample.csv \
    --delimiter , --max-rows 1000 --report transform.json
```

The report lists every column's original name, its canonical mapping, the
mapping method and confidence, and the exact transformations applied — so an
analysis can state what was changed without re-deriving it.

---

## Handing off to GWASLab

```bash
gwaspoker download GCST90271311 -d ./sumstats --gwaslab
```

Optional, and off by default. Requires `pip install -e ".[gwaslab]"`.

!!! note "Why the hand-off is one-directional"

    GWASLab is never called during probing, mapping, value validation or
    readiness assessment. Those must stay independent of it, because GWASLab
    also serves as external ground truth in the
    [benchmark](benchmarking.md) — scoring a parser against labels it
    generated itself measures nothing.

    The hand-off exists so GWASLab receives a file *already known* to be usable.

---

## Next

- [Benchmarking](benchmarking.md) — why the evidence lines stay separate
- [CLI Reference](cli-reference.md#download) — every flag
