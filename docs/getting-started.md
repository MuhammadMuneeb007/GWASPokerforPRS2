# Getting Started

## Install

```bash
git clone https://github.com/MuhammadMuneeb007/GWASPokerforPRS2.git
cd GWASPokerforPRS2
pip install -e .
```

Requires **Python 3.9 or later**. Runs on Windows, Linux and macOS.

!!! info "No shell tools required"

    GWASPoker never calls `wget`, `gunzip`, `zcat`, `tar` or `7z`. Everything —
    including bounded transfers and incremental decompression — happens inside
    Python, so behaviour is identical on all three platforms.

### Optional extras

Each extra is genuinely optional; the core never imports them.

```bash
pip install -e ".[gwaslab]"   # hand off a verified file to GWASLab
pip install -e ".[excel]"     # write .xlsx reports
pip install -e ".[fuzzy]"     # fuzzy column-name suggestions
pip install -e ".[llm]"       # ELECTRA fallback for sample counts
pip install -e ".[dev]"       # pytest, ruff, black
pip install -e ".[all]"       # everything
```

### Verify the install

```bash
gwaspoker --version
gwaspoker --help
```

---

## Three commands to a verdict

### 1. Find studies for a phenotype

```bash
gwaspoker search --trait "coronary artery disease" --sumstats-only --limit 10
```

```text
 GCST          Trait                     File  SSF Meta  Harmonised  GWAS-SSF  PRS  Probe
 GCST90132314  Coronary artery disease   yes   yes       yes         yes       yes  no
 GCST005194    Coronary artery disease   yes   no        yes         ?         ?    yes
```

Read the columns as: does a file exist, is there a `-meta.yaml` sidecar, is a
harmonised version published, does the sidecar declare GWAS-SSF v1.0, is it
PRS-ready on that evidence, and would a byte probe be needed to be sure.

!!! tip "Speed"

    The file/sidecar columns cost two to three requests per study. Use
    `--no-check-files` to skip that stage and return immediately — those columns
    then show `?`, meaning *not checked*, never *absent*.

### 2. Assess one study

```bash
gwaspoker assess GCST90132314 --target prs
```

If the study publishes a GWAS-SSF sidecar, this reads about 1 KB of metadata and
never touches the data file. Otherwise it probes a bounded prefix.

### 3. Download it, if it is worth downloading

```bash
gwaspoker download GCST90132314 --output-dir ./sumstats
gwaspoker extract ./sumstats/GCST90132314.tsv.gz -o clean.tsv --rename
```

`extract` writes a table whose columns carry canonical names, so downstream
tools do not each need their own alias list.

---

## Or do it all at once

```bash
gwaspoker run --trait migraine --limit 10 --target prs
```

`run` searches, assesses every candidate, ranks them by readiness and sample
size, and prints one table. Add `--download` to fetch the winners.

---

## Reading a verdict

GWASPoker returns one of three verdicts.

<div class="grid cards" markdown>

- <span class="verdict-ready">READY</span> — all five required fields are
  present, and the sampled values agree with what the header claimed.

- <span class="verdict-conditional">CONDITIONAL</span> — usable, but something
  must be supplied or derived first. The report names the condition; see
  [PRS Readiness Rules](readiness.md).

- <span class="verdict-not-ready">NOT_READY</span> — a required field is
  missing or contradicted. The report names which.

</div>

A verdict always arrives with its evidence: which route produced it, which
columns mapped to which concepts, how confidently, and whether the values
agreed.

---

## Common first tasks

=== "Check a file you already have"

    ```bash
    gwaspoker scan ./downloaded.tsv.gz
    ```

    No network at all. Works on `.tsv`, `.csv`, `.txt`, `.gz`, `.bgz`, `.zip`,
    `.tar`, `.tar.gz`, `.bz2`.

=== "Check a URL from a consortium site"

    ```bash
    gwaspoker assess https://some-consortium.org/gwas.txt.gz
    ```

    Bounded exactly as an accession is — a direct URL never triggers a full
    download.

=== "Machine-readable output"

    ```bash
    gwaspoker assess GCST90132314 --format json --output verdict.json
    ```

    `--format` also accepts `csv` and `html`. Without `--output`, JSON and CSV
    are written to stdout so they can be piped.

=== "Record provenance"

    ```bash
    gwaspoker assess GCST90132314 --provenance run.json
    ```

    Captures the GWASPoker version, Python version, platform, GWAS Catalog data
    release, EFO version, full configuration and every per-operation fact.

---

## Next

- [Find Studies](search.md) — the search command in depth
- [Assess for PRS](assess.md) — what the verdict is built from
- [CLI Reference](cli-reference.md) — every flag on every command
