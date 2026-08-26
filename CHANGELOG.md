# Changelog

## 2.1.0 — robustness pass before the external rerun

A run of 2.0.0 over 768 heterogeneous summary-statistics URLs produced a
failure taxonomy that, on inspection, was largely **mis-attributed**. This
release fixes the causes, so the rerun measures GWASPoker rather than its
labels. No behaviour that was already correct was changed.

| Reported by 2.0.0 | Actual cause |
| --- | --- |
| 111 "gzip decompression" failures | mostly landing pages served on `.gz` URLs |
| 29 "ZIP decompression" failures | mostly the same |
| 36 valid ustar archives with "no header" | real data, unreachable past metadata members |
| some "successful" headers | HTML fragments (`<meta`, `name=`, `content=`) scored as columns |

### Correctness

* **Suffix heuristics match at a word boundary.** Normalizing the pattern used
  to strip its leading underscore, so `_or` matched the tail of any word ending
  in those letters — this is how the CSS fragment `span{background-color:`
  became an odds ratio, and `FreqSE` a standard error.
* **Payload classification before decoding** (`probe/payload.py`). Evidence is
  weighed strongest-first: magic bytes, then markup sniffing, then content-type,
  then extension mismatch. `application/octet-stream` is explicitly not
  evidence. Real gzip magic outranks a `text/html` header, because servers
  mislabel `.gz` routinely.
* **Two new failure categories**: `non_data_response` (the server returned a
  page, not the file) and `content_mismatch` (the extension disagrees with the
  bytes). Neither is reported as `decompression_error` any more.
* **A content mismatch is not automatically fatal.** `study.txt.gz` that holds
  ordinary TSV is a naming error on the server, over data GWASPoker can read.
  When the bytes are convincingly textual the mismatch is downgraded to a
  warning, compression is set to `NONE`, and encoding, header detection and
  mapping proceed as normal; the warning still reaches the console and
  provenance (`warnings`, `payload.is_textual`). A `.gz` holding unrecognised
  *binary* has nothing to parse and remains a failure. Textuality is decided by
  the conventional NUL/control-byte test, with an explicit exception for
  UTF-16/32 byte-order marks; whether the text is *tabular* is left to header
  detection and the data-table guard, so the two cannot disagree.
* **Archives are walked, not sampled.** Both the tar and zip prefix parsers
  follow the member chain and skip directory records, PAX/GNU headers,
  `__MACOSX/` forks, READMEs and PDFs. Zip is handled from local file headers
  alone — including Zip64 sizes and the streamed form where sizes are deferred
  to a trailing data descriptor — because the central directory sits past every
  probe boundary. Archive-noise matching now distinguishes extensions
  (`endswith`) from documentation names (substring), so `study.results.txt` is
  no longer rejected for containing `.r`.
* **A data-table guard** before a header is accepted: the mapping must resolve,
  or the rows must be arity-consistent and data-like.

### Transport

* **HEAD is advisory.** A HEAD timeout used to abort the whole probe; old
  consortium servers frequently reject HEAD while serving GET perfectly well.
  Failure is recorded and the probe continues.
* **403, 404 and 410 are terminal** — no fallback is attempted, since a second
  request cannot change the answer. Every other status falls through
  HEAD → Range GET → bounded GET.
* **Every attempt is recorded** with method, status, bytes and duration, and
  bytes are accumulated across all of them rather than only the last.
* **Bytes received before a mid-stream failure are counted.** The request and
  the streaming read shared one `try`, so a probe that received 96 KiB and then
  hit a connection reset was booked as having transferred nothing — which
  overstates the transfer reduction GWASPoker reports, and that figure is a
  headline result. `PartialTransferError` now carries the real byte count,
  elapsed time, status, final URL and content-type from the failed attempt, and
  the prober records them. The read buffer is owned by the caller rather than by
  the read helper, so it survives the exception.
* **Response metadata is captured whatever the status**, including the failure
  path: final URL after redirects, redirect count, content-type and
  content-disposition. A 404 reached after two redirects is explained by the
  redirects.

### Input handling

* **`ftp://` is rewritten only for verified mirrors** (`ftp.ebi.ac.uk`,
  `ftp.ncbi.nlm.nih.gov`, `ftp.sanger.ac.uk`, `ftp.1000genomes.ebi.ac.uk`).
  Other hosts are refused with an explanation: `ftp://host/path` does not imply
  `https://host/path`, and guessing turns "unsupported scheme" into a
  misleading 404 against a URL the user never asked for.
* **Dropbox share links are rewritten to `dl=1`** (`url_resolvers.py`), which is
  what made them return an HTML preview instead of the file. Dropbox only —
  there is no evidence for the other providers, and untestable rewrite rules
  would have to be maintained against APIs that change.
* Every rewrite is reported: `normalisation_rule` names the rule and
  `original_url` preserves what was typed.

### Vocabulary

* Added, each verified against the tool that emits it: `P.2gc`, `SE.2gc`,
  `GWAS_P`, `n_total_sum`, `FreqAllele1HapMapCEU` and variants, `mach_r2`,
  `mach_rsq`.
* Deliberately left unmapped, and documented as such: `FreqSE`, `MinFreq`,
  `MaxFreq` (METAL frequency *dispersion*, not a frequency), `Overall`,
  `Direction`, `P_BMD`/`P_LM`/`beta_BMD`/`beta_LM` (multi-analysis files, where
  a blanket rule would silently pick a phenotype), `SNP_hg18`/`SNP_hg19`.

### Command-line behaviour

* **A classified failure no longer surfaces as a Python traceback.** `download`,
  `extract`, `run` and `benchmark` had no `GWASPokerError` handler, so an
  expected outcome — a study with no summary-statistics directory, for
  instance — printed thirty lines of frames and buried the failure category.
  `main()` now catches it, prints the message and the category, and exits 1. A
  genuinely unexpected exception still gets its traceback, because that is a bug
  and the frames are the point.

* **`benchmark --run` no longer overwrites the input manifest.** The write
  destination defaulted to the manifest that was read, so a run without
  `--update-manifest` rewrote its own input — a file whose entire purpose is to
  hold hand-curated ground truth, the one thing in this project GWASPoker must
  never write. Writing back is now opt-in, and the run says so.

### Documentation

* **A documentation site**, built with MkDocs Material and deployed to GitHub
  Pages from `main`: <https://muhammadmuneeb007.github.io/GWASPokerforPRS2/>.
  Nineteen pages covering installation, each command, the mapping and validation
  models, the readiness rules, configuration, failure categories, benchmarking
  and reproducibility.
* **The CLI reference is generated** from the CLI's own `--help`
  (`docs/generate_cli_reference.py`), so it cannot describe a program that does
  not exist.
* **`tests/test_docs.py`** enforces that: every command has a section, every
  flag appears on the published page, every nav entry resolves, every page is
  reachable, every internal link resolves, and every failure category and
  configuration setting named in prose is real.
* `docs/ARCHITECTURE.md`, `MAPPING_SCHEMA.md`, `API_SOURCES.md` and
  `MIGRATION_NOTES.md` were renamed to lower-case site paths
  (`architecture.md`, `mapping.md`, `data-sources.md`, `migration.md`); all
  references were updated.

### Housekeeping

* The User-Agent is derived from `__version__` instead of being repeated as a
  literal, so it cannot misattribute a benchmark run.
* Project URLs corrected to `MuhammadMuneeb007/GWASPokerforPRS2`; they pointed
  at a repository that does not exist.
* **Nothing asserts against rendered `--help` output any more.** Rich truncates
  long option names to fit its table — `--no-check-files` becomes
  `--no-check-fil…` — by an amount that varies with the terminal width, the Rich
  version and the Click version (CI resolved rich 15.0.0 and click 8.4.2 against
  14.2.0 and 8.1.8 locally). A test and the reference generator both parsed that
  output, so both passed locally and failed on CI. Both now read Click's
  parameter objects directly, which is the actual source of truth and is immune
  to rendering. `tests/conftest.py` additionally pins `COLUMNS`.
* GitHub Actions CI added (`.github/workflows/ci.yml`): ruff, black and the unit
  suite on Python 3.9 and 3.13, Linux and Windows, plus a `mkdocs build
  --strict` job, so test and docs status are visible from the repository. The
  live integration suite is `workflow_dispatch` only — running it on every push
  would put avoidable load on ftp.ebi.ac.uk.
* 107 new tests (`tests/test_robustness.py`), organised by the diagnosis each
  one rules out; 5 new fixtures — an HTML landing page, an S3 XML error
  document, a tar and a zip whose data sits behind metadata members, and plain
  text served under a `.gz` name.

## 2.0.0

Initial v2 release.
