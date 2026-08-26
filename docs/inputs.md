# Direct URLs and Local Files

Every command that takes a target accepts the same three forms. There is no
separate `assess-url` command.

```bash
gwaspoker assess GCST90271311                              # GWAS Catalog accession
gwaspoker assess https://consortium.org/gwas.txt.gz        # direct URL
gwaspoker scan   ./downloaded.tsv.gz                       # local file (scan only)
```

---

## Everything after the file is located is identical

```text
accession  ─┐
direct URL ─┼─→ bounded probe → compression → encoding → header → mapping
local file ─┘                 → value validation → PRS readiness → output
```

A direct URL skips the GWAS Catalog resolver. It does not skip, shorten or
weaken the analysis.

!!! important "A direct URL never triggers a full download"

    `--probe-bytes` is the ceiling either way.

    ```bash
    gwaspoker assess https://example.org/huge.gz --probe-bytes 262144
    ```

### Which commands accept which

| Command | Accession | Direct URL | Local path |
| --- | :---: | :---: | :---: |
| `search` | — | — | — |
| `probe` | yes | yes | — |
| `assess` | yes | yes | — |
| `scan` | yes | yes | **yes** |
| `download` | yes | yes | — |
| `extract` | — | — | **yes** |
| `run` | — | — | — |

Classification lives in one module, `inputs.py`, and a test prevents any command
from reimplementing it. That matters: the rule used to be repeated in four
places with three different behaviours, so `assess` accepted `ftp://` and then
crashed inside `requests`, while `download` rejected the same string outright.

---

## URL rewrites are explicit, and never guesses

### `ftp://`

`requests` has no FTP adapter, so `ftp://` must become `https://` to be
fetchable. GWASPoker does that **only for hosts verified to serve the same paths
over both**:

- `ftp.ebi.ac.uk`
- `ftp.ncbi.nlm.nih.gov`
- `ftp.sanger.ac.uk`
- `ftp.1000genomes.ebi.ac.uk`

Any other host is **refused with an explanation**:

```text
'ftp://ftp.example.org/x.tsv.gz' uses ftp://, which GWASPoker cannot fetch (the
HTTP layer has no FTP adapter), and 'ftp.example.org' is not a host known to
serve the same paths over HTTPS. Supply the https:// URL directly, or add the
host to FTP_HTTPS_MIRRORS once you have verified that its HTTPS paths mirror
its FTP paths.
```

!!! question "Why not just try `https://` and see?"

    Because `ftp://host/path` does not imply `https://host/path`. Many FTP
    servers have no HTTP front end; some that do use a different path prefix.
    Guessing turns an honest *unsupported scheme* into a misleading **404
    against a URL the user never asked for** — which is worse, because it looks
    like a finding about the file.

### Share links

A Dropbox URL ending `.zip` returns an HTML preview page, not the file, unless
`dl=1` is set. GWASPoker sets it.

**Only Dropbox is implemented.** OneDrive, SharePoint and Google Drive are
deliberately absent: there is no failing example to test against, and rewrite
rules for hosts with no evidence would be speculative, untestable, and would
have to be maintained against APIs that change.

---

## The rewrite is always reported

```json
{
  "input_type": "direct_url",
  "input": {
    "input": "https://www.dropbox.com/s/abc/gwas.txt.gz?dl=0",
    "original_url": "https://www.dropbox.com/s/abc/gwas.txt.gz?dl=0",
    "url": "https://www.dropbox.com/s/abc/gwas.txt.gz?dl=1",
    "normalisation_rule": "dropbox_direct_download",
    "accession": null
  }
}
```

`original_url` preserves what was typed; `normalisation_rule` names the rule
that fired. A supplementary table can therefore state exactly which URLs were
altered and why.

---

## The source type is recorded

`input_type` is carried into every report and provenance file, so an
external-validation experiment can separate catalogue studies from arbitrary
public URLs without re-parsing the target string.

```json
{ "input_type": "gwas_catalog_accession",
  "input": { "input": "GCST90271311", "accession": "GCST90271311", "url": null } }
```

Note what differs between the routes on the *same* file: an accession can reach
a verdict through the GWAS-SSF sidecar with **zero data bytes**, while a direct
URL has no catalogue metadata and must be probed. Both can reach `READY`;
`readiness_evidence_source` records which route got there.

---

## Next

- [Inspect a File](probe-scan.md) — what the probe reads
- [Failure Categories](failures.md) — what a refusal means
