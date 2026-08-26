# CLI Reference

!!! note "Generated"

    This page is produced from the CLI's own parameter definitions by
    `docs/generate_cli_reference.py`, so it cannot drift from the program.
    Generated for GWASPoker **2.1.0**.

```bash
gwaspoker --help          # list commands
gwaspoker COMMAND --help  # options for one command
gwaspoker --version
```

## Commands at a glance

| Command | Purpose | Network |
| --- | --- | --- |
| [`search`](#search) | Find studies for a phenotype | yes |
| [`probe`](#probe) | Read a remote header, bounded | yes |
| [`assess`](#assess) | PRS-readiness verdict | yes |
| [`scan`](#scan) | Inspect a local file | no, for a local path |
| [`download`](#download) | Fetch the complete file | yes |
| [`extract`](#extract) | Normalize a downloaded file | no |
| [`run`](#run) | Search, assess and rank | yes |
| [`benchmark`](#benchmark) | Score against ground truth | yes, with `--run` |

## Options shared by every command

| Flag | Purpose |
| --- | --- |
| `--config PATH` | A `gwaspoker.toml` or `.yaml` config file. See [Configuration](configuration.md). |
| `--failure-log PATH` | Append classified failures as JSON Lines. |
| `-v`, `-vv` | INFO, then DEBUG logging. |
| `-q`, `--quiet` | Only report errors. |
| `--help` | Show the command's options and exit. |

---

## `search`

Find GWAS Catalog studies for a phenotype.

```text
gwaspoker search [OPTIONS]
```

Two stages. The first asks the GWAS Catalog for studies matching the trait; the
second checks, per study, whether a file exists, whether a `-meta.yaml` sidecar
exists, whether a harmonised version is published and what `file_type` the
sidecar declares. The second stage costs two to three requests per study and is
parallelised across `--workers` threads that share one process-wide rate
limiter, so it overlaps latency without raising the request rate.

`--no-check-files` skips the second stage entirely. Those columns then read `?`,
which means *not checked* — never *absent*.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `--trait`, `-t` | `str` | **required** Phenotype or trait to search for. |
| `--population`, `-p` | `str` | Ancestry filter, e.g. European, East Asian. |
| `--limit`, `-n` | `int` | Maximum studies to return.<br>_Default: `25`_ |
| `--sumstats-only` |  | Only studies with full summary statistics. |
| `--check-files`, `--no-check-files` |  | Look up File / SSF Meta / Harmonised / GWAS-SSF / PRS / Probe for each result. Two or three requests per study; --no-check-files leaves those columns as '?' and returns immediately.<br>_Default: `check-files`_ |
| `--workers`, `-w` | `int range 1..16` | Threads for the file-availability stage (default 6). All workers share one process-wide rate limiter, so this overlaps latency rather than raising the request rate. |
| `--exclude` | `str` | Drop studies whose reported trait contains this text (case-insensitive, repeatable). |
| `--default-excludes`, `--no-default-excludes` |  | Also exclude 'Gene-based burden', which are not variant-level summary statistics and cannot yield PRS weights.<br>_Default: `default-excludes`_ |
| `--llm`, `--no-llm` |  | Allow the ELECTRA fallback for unresolved sample counts.<br>_Default: `no-llm`_ |
| `--show-provenance` |  | Print where each sample count came from. |
| `--format`, `-f` | `str` | table, csv, json or html.<br>_Default: `table`_ |
| `--output`, `-o` | `path` | Write results to this path. |
| `--provenance` | `path` | Write a provenance JSON file. |

---

## `probe`

Inspect a remote file's header without downloading it.

```text
gwaspoker probe [OPTIONS]
```

Fetches a bounded prefix and reports what is in it: compression, encoding,
delimiter, the header row, and the canonical mapping. Never reaches a PRS
verdict — use `assess` for that.

The byte ceiling is `--probe-bytes`, and it is a ceiling in every path: HTTP
`Range` when the server supports it, a stream closed early when it does not.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `target` | `str` | **required** A GCST accession or an http(s) URL. |
| `--probe-bytes` | `int` | Bytes to inspect. Suggested: 65536, 131072, 262144, 524288, 1048576. |
| `--harmonised` | `str` | auto (default), yes or no -- prefer the harmonised file. |
| `--format`, `-f` | `str` | table, csv, json or html.<br>_Default: `table`_ |
| `--output`, `-o` | `path` | Write results to this path. |
| `--provenance` | `path` | Write a provenance JSON file. |

---

## `assess`

Decide whether a study's summary statistics are usable for PRS.

```text
gwaspoker assess [OPTIONS]
```

The main command. Prefers structured metadata: when a `-meta.yaml` sidecar
declares `file_type: GWAS-SSF v1.0`, the schema is fixed by the standard and the
verdict needs **no data bytes at all**. Otherwise it falls back to a bounded
probe.

`--force-probe` reads bytes even when metadata would have sufficed — useful for
verifying that the two routes agree. `--no-api` disables the metadata route
entirely.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `targets` | `str` | **required** One or more GCST accessions or URLs. |
| `--target` | `str` | Downstream workflow. Currently: prs.<br>_Default: `prs`_ |
| `--force-probe` |  | Probe the file even when the structured metadata was sufficient. Needed to compare the two routes in a benchmark. |
| `--no-api` |  | Skip the structured route and go straight to the probe. |
| `--probe-bytes` | `int` | Bytes to inspect. Suggested: 65536, 131072, 262144, 524288, 1048576. |
| `--harmonised` | `str` | auto (default), yes or no -- prefer the harmonised file. |
| `--show-mapping` |  | Print the column mapping. |
| `--format`, `-f` | `str` | table, csv, json or html.<br>_Default: `table`_ |
| `--output`, `-o` | `path` | Write results to this path. |
| `--provenance` | `path` | Write a provenance JSON file. |

---

## `scan`

Report format, compression, encoding, delimiter, header and PRS fields.

```text
gwaspoker scan [OPTIONS]
```

The offline command. Takes a local path and needs no network. Also accepts an
accession or URL, in which case it behaves like `probe` with a readiness verdict
attached.

`--emit-code` prints a ready-to-paste `pandas.read_csv` call with the detected
delimiter, encoding, header row and comment character already filled in.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `target` | `str` | **required** A local file path, an http(s) URL or a GCST accession. |
| `--probe-bytes` | `int` | Bytes to inspect. Suggested: 65536, 131072, 262144, 524288, 1048576. |
| `--target` | `str` | Downstream workflow.<br>_Default: `prs`_ |
| `--emit-code` |  | Print a pandas snippet that renames columns to PRS symbols. |
| `--format`, `-f` | `str` | table, csv, json or html.<br>_Default: `table`_ |
| `--output`, `-o` | `path` | Write results to this path. |

---

## `download`

Download the complete file, with checksum verification.

```text
gwaspoker download [OPTIONS]
```

Transfers the complete file and verifies it against the published MD5. Resumes
an interrupted transfer when the server supports ranges.

`--no-verify` skips checksum verification; use it only when the source publishes
no checksum.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `target` | `str` | **required** A GCST accession or an http(s) URL. |
| `--output-dir`, `-d` | `path` | Directory to download into. |
| `--harmonised` | `str` | auto (default), yes or no -- prefer the harmonised file. |
| `--overwrite` |  | Replace an existing file. |
| `--no-verify` |  | Skip MD5 verification against the published checksum. |
| `--gwaslab` |  | Hand the downloaded file to GWASLab afterwards. |
| `--output`, `-o` | `path` | Write results to this path. |
| `--provenance` | `path` | Write a provenance JSON file. |

---

## `extract`

Decompress and normalize a downloaded file into a clean table.

```text
gwaspoker extract [OPTIONS]
```

Decompresses and writes a clean table. **Only declared transformations are
applied**, and every one appears in the report: GWASPoker never rewrites data
values to make a parser succeed.

`--rename` gives columns their canonical concept names; `--rename-symbols` gives
them the short forms PRS tools expect (`CHR`, `BP`, `A1`, `A2`, `BETA`, `SE`,
`P`).

An archive member is unpacked into a sibling `<name>_extracted/` directory
beside the **input** file, not beside `--output`.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `path` | `path` | **required** A downloaded summary-statistics file. |
| `--output`, `-o` | `path` | Output file path. |
| `--delimiter` | `str` | Output delimiter.<br>_Default: `	`_ |
| `--max-rows` | `int` | Read at most this many rows. |
| `--rename` |  | Rename columns to canonical concepts. |
| `--rename-symbols` |  | Rename columns to PRS tool symbols (CHR, BP, A1, ...). |
| `--overwrite` |  | Replace an existing output file. |
| `--report` | `path` | Write the transformation report as JSON. |

---

## `run`

Search, assess and rank candidate studies end to end.

```text
gwaspoker run [OPTIONS]
```

Search, assess and rank in one pass. Ranks candidates by readiness first, then
by sample size. Add `--download` to fetch the studies that came back `READY`.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `--trait`, `-t` | `str` | **required** Phenotype to search for. |
| `--population`, `-p` | `str` | Ancestry filter. |
| `--target` | `str` | Downstream workflow.<br>_Default: `prs`_ |
| `--limit`, `-n` | `int` | Studies to assess.<br>_Default: `10`_ |
| `--force-probe` |  | Probe even when the API sufficed. |
| `--download` |  | Download the top-ranked study. Files can be many gigabytes. |
| `--gwaslab` |  | Run GWASLab on any downloaded file. |
| `--output-dir`, `-d` | `path` | Download directory. |
| `--probe-bytes` | `int` | Bytes to inspect. Suggested: 65536, 131072, 262144, 524288, 1048576. |
| `--harmonised` | `str` | auto (default), yes or no -- prefer the harmonised file. |
| `--format`, `-f` | `str` | table, csv, json or html.<br>_Default: `table`_ |
| `--output`, `-o` | `path` | Write results to this path. |
| `--provenance` | `path` | Write a provenance JSON file. |

---

## `benchmark`

Score predictions against externally curated ground truth.

```text
gwaspoker benchmark [OPTIONS]
```

Scores predictions against a manifest of externally curated ground truth.

!!! warning "Ground truth must come from somewhere else"

    GWASPoker never writes the ground-truth columns. Scoring a parser against
    labels it produced itself measures nothing, and the evaluator warns when a
    manifest looks like that has happened. See
    [Benchmarking](benchmarking.md).

    `--run` keeps predictions in memory; it does **not** write them back over
    the manifest you gave it. Pass `--update-manifest PATH` to save them
    somewhere.

### Options

| Flag | Type | Description |
| --- | --- | --- |
| `manifest_path` | `path` | **required** Benchmark manifest CSV. |
| `--run` |  | Run GWASPoker to fill the prediction columns first. |
| `--probe-size-sweep` |  | Probe each study at 64 KB, 128 KB, 256 KB, 512 KB and 1 MB. |
| `--stratify` | `str` | Comma-separated: ssf_status, api_coverage, file_format, compression, source. |
| `--probe-bytes` | `int` | Bytes to inspect. Suggested: 65536, 131072, 262144, 524288, 1048576. |
| `--harmonised` | `str` | auto (default), yes or no -- prefer the harmonised file. |
| `--update-manifest` | `path` | Write the manifest back with predictions filled in. |
| `--output`, `-o` | `path` | Write results to this path. |

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. A `NOT_READY` verdict is still a success: the question was answered. |
| `1` | The operation failed — the study was not found, the file was unreachable, no header could be recovered. The failure category is printed and, with `--failure-log`, recorded. |
| `2` | The command line itself was wrong — an unknown flag, a missing required argument, a value outside its allowed range. |

