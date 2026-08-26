"""Regenerate ``docs/cli-reference.md`` from the CLI's own parameter objects.

Hand-written flag tables drift the moment an option is added, so this page is
generated. It reads Click's parameter objects directly rather than parsing the
rendered ``--help`` output: Rich sizes and *truncates* that output, so
``--no-check-files`` becomes ``--no-check-fil…`` at some widths and Rich
versions, and a page generated on one machine disagreed with the same page
generated on another.

Run it from the repository root after changing any command signature::

    python docs/generate_cli_reference.py

``tests/test_docs.py`` fails if you forget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import typer.main

from gwaspoker import __version__
from gwaspoker.cli import app

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

#: Options every command inherits, documented once in a shared table.
SHARED = frozenset({"--config", "--failure-log", "--verbose", "--quiet", "--help"})

#: A short note on what each command is actually for, beyond the one-liner.
NOTES = {
    "search": """Two stages. The first asks the GWAS Catalog for studies matching the trait; the
second checks, per study, whether a file exists, whether a `-meta.yaml` sidecar
exists, whether a harmonised version is published and what `file_type` the
sidecar declares. The second stage costs two to three requests per study and is
parallelised across `--workers` threads that share one process-wide rate
limiter, so it overlaps latency without raising the request rate.

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
`P`).

An archive member is unpacked into a sibling `<name>_extracted/` directory
beside the **input** file, not beside `--output`.""",
    "run": """Search, assess and rank in one pass. Ranks candidates by readiness first, then
by sample size. Add `--download` to fetch the studies that came back `READY`.""",
    "benchmark": """Scores predictions against a manifest of externally curated ground truth.

!!! warning "Ground truth must come from somewhere else"

    GWASPoker never writes the ground-truth columns. Scoring a parser against
    labels it produced itself measures nothing, and the evaluator warns when a
    manifest looks like that has happened. See
    [Benchmarking](benchmarking.md).

    `--run` keeps predictions in memory; it does **not** write them back over
    the manifest you gave it. Pass `--update-manifest PATH` to save them
    somewhere.""",
}


def escape(text: str) -> str:
    """Make a string safe inside a Markdown table cell."""
    return text.replace("|", "&#124;").replace("\n", " ").strip()


def type_label(param: click.Parameter) -> str:
    """A readable type, including the bounds of a constrained range.

    Attributes rather than ``isinstance``: Typer ships its own subclasses
    (``typer._click.types.IntRange``), so checking against Click's own classes
    silently drops the bounds.
    """
    kind = param.type
    if getattr(param, "is_flag", False) or kind.name == "boolean":
        return ""
    name = kind.name
    low, high = getattr(kind, "min", None), getattr(kind, "max", None)
    if low is not None or high is not None:
        name = f"{name} {low if low is not None else '-'}..{high if high is not None else '-'}"
    return f"`{name}`"


def default_label(param: click.Parameter) -> str:
    value = param.default
    if value is None or param.required:
        return ""
    if isinstance(value, bool):
        # A boolean flag pair reads better as which half is on by default.
        if param.secondary_opts:
            on = param.opts[0] if value else param.secondary_opts[0]
            return f"<br>_Default: `{on.lstrip('-')}`_"
        return "" if value is False else "<br>_Default: `on`_"
    if callable(value):
        return ""
    return f"<br>_Default: `{value}`_"


def flag_label(param: click.Parameter) -> str:
    names = list(param.opts) + list(param.secondary_opts)
    return ", ".join(f"`{n}`" for n in names)


def md_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    return (
        "| " + " | ".join(headers) + " |\n"
        "| " + " | ".join("---" for _ in headers) + " |\n" + body
    )


def command_sections(command: click.Command) -> list[str]:
    arguments: list[tuple[str, ...]] = []
    options: list[tuple[str, ...]] = []

    for param in command.params:
        if isinstance(param, click.Argument):
            help_text = escape(getattr(param, "help", "") or "")
            required = "**required** " if param.required else ""
            arguments.append((f"`{param.name}`", type_label(param), required + help_text))
            continue
        if set(param.opts) & SHARED:
            continue
        help_text = escape(getattr(param, "help", "") or "")
        required = "**required** " if param.required else ""
        options.append(
            (flag_label(param), type_label(param), required + help_text + default_label(param))
        )

    parts: list[str] = []
    if arguments:
        parts += ["### Arguments", "", md_table(("Argument", "Type", "Description"), arguments), ""]
    if options:
        parts += ["### Options", "", md_table(("Flag", "Type", "Description"), options), ""]
    return parts


def main() -> None:
    root: Any = typer.main.get_command(app)

    parts = [
        "# CLI Reference",
        "",
        '!!! note "Generated"',
        "",
        "    This page is produced from the CLI's own parameter definitions by",
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
                (
                    "`--config PATH`",
                    "A `gwaspoker.toml` or `.yaml` config file. "
                    "See [Configuration](configuration.md).",
                ),
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
        command = root.commands[name]
        usage = f"gwaspoker {name} [OPTIONS]"
        positional = [p.name for p in command.params if isinstance(p, click.Argument)]
        if positional:
            usage += " " + " ".join(p.upper() for p in positional)

        parts += [f"## `{name}`", "", summary, "", "```text", usage, "```", ""]
        if name in NOTES:
            parts += [NOTES[name], ""]
        parts += command_sections(command)
        parts += ["---", ""]

    parts += [
        "## Exit codes",
        "",
        md_table(
            ("Code", "Meaning"),
            [
                (
                    "`0`",
                    "Success. A `NOT_READY` verdict is still a success: "
                    "the question was answered.",
                ),
                (
                    "`1`",
                    "The operation failed — the study was not found, the file was "
                    "unreachable, no header could be recovered. The failure category is "
                    "printed and, with `--failure-log`, recorded.",
                ),
                (
                    "`2`",
                    "The command line itself was wrong — an unknown flag, a missing "
                    "required argument, a value outside its allowed range.",
                ),
            ],
        ),
        "",
    ]

    OUTPUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
