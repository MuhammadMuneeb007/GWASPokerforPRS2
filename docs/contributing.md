# Contributing

## Setting up

```bash
git clone https://github.com/MuhammadMuneeb007/GWASPokerforPRS2.git
cd GWASPokerforPRS2
pip install -e ".[dev]"
```

## Before every push

```bash
python -m ruff check src tests
python -m black --check src tests
python -m pytest -q
```

CI runs exactly these, on Python 3.9 and 3.13, on Linux and Windows.

The live integration suite is not run in CI on every push — it hits
`ftp.ebi.ac.uk` and would put load on someone else's server:

```bash
python -m pytest -q -m integration
```

Run it before a release, and whenever you touch the HTTP or catalog layers.

---

## Rules the tests enforce

These are checked by an AST walk over the source, not by review.

| Rule | Why |
| --- | --- |
| No bare `except:`, no `except Exception: pass` | A swallowed exception becomes a wrong answer with no trace. |
| No `subprocess`, no `os.system` | v1 shelled out to `wget`, `gunzip` and `7z`; the result did not run on Windows. |
| A failure never returns a plausible-looking value | Better to say "unknown" than to be confidently wrong. |
| An alias belongs to exactly one concept | Enforced at YAML load time. |
| No alias is an unvetted concatenation of two others | v1's missing-comma signature, which silently corrupted five aliases. |

---

## Changing the column vocabulary

`src/gwaspoker/mapping/aliases.yaml` is the single source of truth. It is data,
not code.

!!! warning "Add an alias only when you can name its source"

    Every alias should be traceable to the tool or consortium that emits it —
    METAL, BOLT-LMM, REGENIE, PLINK, SAIGE, the GWAS Catalog. "It looks like it
    means that" is not a source.

    Not mapping a column is a legitimate result. `FreqSE` stays unmapped
    because it is METAL's *dispersion* of a frequency across cohorts, not a
    standard error of an effect — mapping it would be worse than leaving it
    `unknown`.

After editing, run the suite: the invariants above are checked at load time, so
a conflict fails immediately.

---

## Changing the CLI

The reference page is generated. After adding or renaming any option:

```bash
python docs/generate_cli_reference.py
```

`tests/test_docs.py` fails if you forget — it reads every command's real
`--help` and checks each flag appears on the published page.

---

## Working on the documentation

```bash
pip install -r requirements-docs.txt
mkdocs serve          # live preview at http://127.0.0.1:8000
mkdocs build --strict # what CI runs
```

`--strict` turns a broken internal link into a build failure. `tests/test_docs.py`
additionally checks that every page is reachable from the nav, that no nav entry
points at a missing file, and that the failure categories and configuration
settings named in prose actually exist.

The site deploys automatically from `main` via `.github/workflows/pages.yml`.

---

## Adding a fixture

```bash
python tests/fixtures/_generate.py
```

Fixtures are committed as bytes so they are reviewable. Each one targets a
specific failure mode — a `#` preamble, a 25-line metadata block, Latin-1, a
UTF-8 BOM, a truncated gzip, an archive whose data sits behind metadata members.
Add one when you fix a bug, and name in its comment what it pins.

---

## Commit conventions

- Keep the working tree clean: `ruff`, `black` and `pytest` all pass.
- Do not commit `site/` — it is generated and gitignored.
- Update `CHANGELOG.md` for anything a user would notice.
