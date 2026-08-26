"""Regenerate ``docs/cli-reference.md`` from the CLI's own ``--help`` output.

Hand-written flag tables drift the moment an option is added. This reads the
help text Typer produces, so the page cannot disagree with the program.

Run it from the repository root after changing any command signature::

    python docs/generate_cli_reference.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTPUT = REPO / "docs" / "cli-reference.md"

COMMANDS = [
    ("search", "Find GWAS Catalog studies for a phenotype."),
    ("probe", "Inspect a remote file's header without downloading it."),
    ("assess", "Decide whether a study's summary statistics are usable for PRS."),
    ("scan", "Report format, compression, encoding, delimiter, header and PRS fields."),
    ("download", "Download the complete file, with checksum verification."),
    ("extract", "Decompress and normalize a downloaded file into a clean table."),
    ("run", "Search, assess and rank candidate studies end to end."),
    ("benchmark", "Score predictions against externally curated ground truth."),
]

#: A short note on what each command is actually for, beyond the one-liner.
NOTES = {
    "search": """Two stages. The first asks the GWAS Catalog for studies matching the
trait; the second checks, per study, whether a file exists, whether a
`-meta.yaml` sidecar exists, whether a harmonised version is published and what
`file_type` the sidecar declares. The second stage costs two to three requests
per study and is parallelised across `--workers` threads that share one
process-wide rate limiter, so it overlaps latency without raising the request
rate.

`--no-check-files` skips the second stage entirely. Those columns then read `?`,
which means *not checked* — never *absent*.""",
    "probe": """Fetches a bounded prefix and reports what is in it: compression, encoding,
delimiter, the header row, and the canonical mapping. Never reaches a PRS
verdict — use `assess` for that.

The byte ceiling is `--probe-bytes`, and it is a ceiling in every path: HTTP
`Range` when the server supports it, a stream closed early when it does not.""",
    "assess": """The main command. Prefers structured metadata: when a `-meta.yaml` sidecar
declares `file_type: GWAS-SSF v1.0`, the schema is fixed by the standard and the
verdict needs **no data bytes at all**. Otherwise it falls back to a bounded
probe.

`--force-probe` reads bytes even when metadata would have sufficed — useful for
verifying that the two routes agree. `--no-api` disables the metadata route
entirely.""",
    "scan": """The offline command. Takes a local path and needs no network. Also accepts an
accession or URL, in which case it behaves like `probe` with a readiness verdict
attached.

`--emit-code` prints a ready-to-paste `pandas.read_csv` call with the detected
delimiter, encoding, header row and comment character already filled in.""",
    "download": """Transfers the complete file and verifies it against the published MD5. Resumes
an interrupted transfer when the server supports ranges.

`--no-verify` skips checksum verification; use it only when the source publishes
no checksum.""",
    "extract": """Decompresses and writes a clean table. **Only declared transformations are
applied**, and every one appears in the report: GWASPoker never rewrites data
values to make a parser succeed.

`--rename` gives columns their canonical concept names; `--rename-symbols` gives
them the short forms PRS tools expect (`CHR`, `BP`, `A1`, `A2`, `BETA`, `SE`,
`P`).""",
    "run": """Search, assess and rank in one pass. Ranks candidates by readiness first, then
by sample size. Add `--download` to fetch the studies that came back `READY`.""",
    "benchmark": """Scores predictions against a manifest of externally curated ground truth.

!!! warning "Ground truth must come from somewhere else"

    GWASPoker never writes the ground-truth columns. Scoring a parser against
    labels it produced itself measures nothing, and the evaluator warns when a
    manifest looks like that has happened. See
    [Benchmarking](benchmarking.md).""",
}

BOX = re.compile(r"[│┌┐└┘─╭╮╰╯├┤┬┴┼]")


def clean(text: str) -> list[str]:
    """Strip Rich's box drawing and collapse the result to plain lines."""
    lines = []
    for raw in text.splitlines():
        line = BOX.sub(" ", raw).rstrip()
        lines.append(line)
    return lines


def section(lines: list[str], title: str) -> list[str]:
    """Return the lines under a Rich panel heading such as ``Options``."""
    out: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped == title:
            collecting = True
            continue
        if collecting:
            if stripped in ("Options", "Arguments", "Commands") and stripped != title:
                break
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse_rows(lines: list[str]) -> list[tuple[str, str, str]]:
    """Turn help lines into ``(flags, type, description)`` triples.

    Continuation lines -- Rich wraps long descriptions -- are folded into the
    row above rather than becoming rows of their own.
    """
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        match = re.match(
            r"^\s*(\*?)\s*(--?[\w-]+(?:\s+--?[\w-]+)*)\s{2,}(<[^>]*>(?:\s*\[[^\]]*\])?)?\s*(.*)$",
            line,
        )
        if match:
            required, flags, kind, desc = match.groups()
            flags = re.sub(r"\s+", " ", flags.strip())
            label = ("**required** " if required else "") + desc.strip()
            rows.append((flags, (kind or "").strip(), label))
        elif rows:
            flags, kind, desc = rows[-1]
            rows[-1] = (flags, kind, (desc + " " + line.strip()).strip())
    return rows


def parse_arguments(lines: list[str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        match = re.match(r"^\s*(\*?)\s*(\{?[\w_]+\}?)\s{2,}(<[^>]*>)?\s*(.*)$", line)
        if match:
            required, name, kind, desc = match.groups()
            name = name.strip("{}")
            label = ("**required** " if required else "") + desc.strip()
            rows.append((name, (kind or "").strip(), label))
        elif rows:
            name, kind, desc = rows[-1]
            rows[-1] = (name, kind, (desc + " " + line.strip()).strip())
    return rows


def flag_cell(value: str) -> str:
    """Render the flag column as code, one span per alias."""
    value = value.strip()
    if not value or value.startswith(("`", "[")):
        return value
    return ", ".join(f"`{part}`" for part in value.split())


def type_cell(value: str) -> str:
    """Render the type column, whose angle brackets Markdown would eat as HTML."""
    value = re.sub(r"\s+", " ", value).strip()
    return f"`{value}`" if value else ""


#: Typer prints `[default: x]`, and Rich may fold the line before its closing
#: bracket. Both shapes are normalised to one italic sentence.
_DEFAULT_CLOSED = re.compile(r"\[default:\s*([^\]]*)\]")
_DEFAULT_OPEN = re.compile(r"\[default:\s*(.*)$")


def desc_cell(value: str) -> str:
    value = value.replace("|", "&#124;").replace("[required]", "").strip()
    value = _DEFAULT_CLOSED.sub(lambda m: f"<br>_Default: `{m.group(1).strip()}`_", value)
    value = _DEFAULT_OPEN.sub(
        lambda m: f"<br>_Default: `{m.group(1).strip().rstrip(']')}`_", value
    )
    return re.sub(r"\s{2,}", " ", value).strip()


def md_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    structured = headers[0] in ("Flag", "Argument")

    def render(row: tuple[str, ...]) -> str:
        cells = list(row)
        if structured and len(cells) == 3:
            cells = [flag_cell(cells[0]), type_cell(cells[1]), desc_cell(cells[2])]
        else:
            cells = [c.replace("|", "&#124;") for c in cells]
        return "| " + " | ".join(cells) + " |"

    body = "\n".join(render(row) for row in rows if any(row))
    return (
        "| " + " | ".join(headers) + " |\n"
        "| " + " | ".join("---" for _ in headers) + " |\n" + body
    )


def help_for(*args: str) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", COLUMNS="400", TERM="dumb")
    proc = subprocess.run(
        [sys.executable, "-m", "gwaspoker.cli", *args, "--help"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )
    return proc.stdout


def main() -> None:
    from gwaspoker import __version__

    parts = [
        "# CLI Reference",
        "",
        "!!! note \"Generated\"",
        "",
        "    This page is produced from the CLI's own `--help` output by",
        "    `docs/generate_cli_reference.py`, so it cannot drift from the program.",
        f"    Generated for GWASPoker **{__version__}**.",
        "",
        "```bash",
        "gwaspoker --help          # list commands",
        "gwaspoker COMMAND --help  # options for one command",
        "gwaspoker --version",
        "```",
        "",
        "## Commands at a glance",
        "",
        md_table(
            ("Command", "Purpose", "Network"),
            [
                ("[`search`](#search)", "Find studies for a phenotype", "yes"),
                ("[`probe`](#probe)", "Read a remote header, bounded", "yes"),
                ("[`assess`](#assess)", "PRS-readiness verdict", "yes"),
                ("[`scan`](#scan)", "Inspect a local file", "no, for a local path"),
                ("[`download`](#download)", "Fetch the complete file", "yes"),
                ("[`extract`](#extract)", "Normalize a downloaded file", "no"),
                ("[`run`](#run)", "Search, assess and rank", "yes"),
                ("[`benchmark`](#benchmark)", "Score against ground truth", "yes, with `--run`"),
            ],
        ),
        "",
        "## Options shared by every command",
        "",
        md_table(
            ("Flag", "Purpose"),
            [
                ("`--config PATH`", "A `gwaspoker.toml` or `.yaml` config file. See [Configuration](configuration.md)."),
                ("`--failure-log PATH`", "Append classified failures as JSON Lines."),
                ("`-v`, `-vv`", "INFO, then DEBUG logging."),
                ("`-q`, `--quiet`", "Only report errors."),
                ("`--help`", "Show the command's options and exit."),
            ],
        ),
        "",
        "---",
        "",
    ]

    for name, summary in COMMANDS:
        text = clean(help_for(name))
        usage = next((line.strip() for line in text if line.strip().startswith("Usage:")), "")
        usage = usage.replace("python -m gwaspoker.cli", "gwaspoker").replace("{", "").replace("}", "")

        parts += [f"## `{name}`", "", summary, "", "```text", usage, "```", ""]
        if name in NOTES:
            parts += [NOTES[name], ""]

        arguments = parse_arguments(section(text, "Arguments"))
        if arguments:
            parts += ["### Arguments", "", md_table(("Argument", "Type", "Description"), arguments), ""]

        options = parse_rows(section(text, "Options"))
        if options:
            parts += ["### Options", "", md_table(("Flag", "Type", "Description"), options), ""]

        parts += ["---", ""]

    parts += [
        "## Exit codes",
        "",
        md_table(
            ("Code", "Meaning"),
            [
                ("`0`", "Success. A `NOT_READY` verdict is still a success: the question was answered."),
                ("`1`", "The operation failed — the study was not found, the file was unreachable, no header could be recovered. The failure category is printed and, with `--failure-log`, recorded."),
                ("`2`", "The command line itself was wrong — an unknown flag, a missing required argument, a value outside its allowed range."),
            ],
        ),
        "",
    ]

    OUTPUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO)} ({len(parts)} blocks)")


if __name__ == "__main__":
    main()
