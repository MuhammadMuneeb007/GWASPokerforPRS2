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
  bytes). Neither is reported as `decompression_error` any more, and a `.gz`
  that is really plain text is now read as plain text.
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

### Housekeeping

* The User-Agent is derived from `__version__` instead of being repeated as a
  literal, so it cannot misattribute a benchmark run.
* 78 new tests (`tests/test_robustness.py`), organised by the diagnosis each
  one rules out; 5 new fixtures — an HTML landing page, an S3 XML error
  document, a tar and a zip whose data sits behind metadata members, and plain
  text served under a `.gz` name.

## 2.0.0

Initial v2 release.
