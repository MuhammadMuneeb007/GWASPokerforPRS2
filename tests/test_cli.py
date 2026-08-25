"""CLI wiring, configuration precedence and reporting.

These tests exercise the command surface without a network: every command is
reachable, options validate, and the offline commands (``scan``, ``extract``)
produce their reports.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from gwaspoker import __version__
from gwaspoker.cli import app
from gwaspoker.config import DEFAULT_PROBE_BYTES, GWASPokerConfig, load_config

runner = CliRunner()


# ----------------------------------------------------------------------
# Command surface
# ----------------------------------------------------------------------


def test_help_lists_every_required_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("search", "probe", "assess", "download", "extract", "scan", "run", "benchmark"):
        assert command in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.parametrize(
    "command", ["search", "probe", "assess", "download", "extract", "scan", "run", "benchmark"]
)
def test_each_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0


def test_python_dash_m_entry_point_exists() -> None:
    import gwaspoker.__main__ as entry

    assert callable(entry.main)


# ----------------------------------------------------------------------
# Option validation
# ----------------------------------------------------------------------


def test_invalid_harmonised_choice_is_rejected() -> None:
    result = runner.invoke(app, ["probe", "GCST1", "--harmonised", "maybe"])
    assert result.exit_code != 0
    assert "auto, yes, no" in result.output


def test_invalid_format_choice_is_rejected() -> None:
    result = runner.invoke(app, ["search", "--trait", "x", "--format", "xml"])
    assert result.exit_code != 0


def test_scan_rejects_a_target_that_is_neither_file_url_nor_accession() -> None:
    result = runner.invoke(app, ["scan", "definitely not a thing"])
    assert result.exit_code == 1
    assert "not an existing file" in result.output


# ----------------------------------------------------------------------
# Offline commands
# ----------------------------------------------------------------------


def test_scan_a_local_file(fixtures_dir) -> None:
    result = runner.invoke(app, ["scan", str(fixtures_dir / "comment_preamble.tsv")])
    assert result.exit_code == 0
    assert "CHR" in result.output
    assert "PRS readiness" in result.output
    assert "READY" in result.output


def test_scan_writes_json(fixtures_dir, tmp_path) -> None:
    out = tmp_path / "scan.json"
    result = runner.invoke(
        app,
        ["scan", str(fixtures_dir / "ssf_like.tsv.gz"), "--format", "json", "--output", str(out)],
    )
    assert result.exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["report_type"] == "scan"
    assert payload["results"]["probe"]["compression"] == "gzip"
    assert payload["results"]["readiness"]["verdict"] == "READY"


def test_scan_writes_mapping_csv(fixtures_dir, tmp_path) -> None:
    import pandas as pd

    out = tmp_path / "mapping.csv"
    result = runner.invoke(
        app,
        [
            "scan",
            str(fixtures_dir / "comment_preamble.tsv"),
            "--format",
            "csv",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0
    frame = pd.read_csv(out)
    assert list(frame["raw_name"]) == ["CHR", "POS", "SNP", "A1", "A2", "BETA", "SE", "P"]
    assert frame["canonical_name"].iloc[0] == "chromosome"


def test_scan_emits_pandas_code_locally(fixtures_dir) -> None:
    """v1 shipped this to HuggingChat and needed an account to run at all."""
    result = runner.invoke(app, ["scan", str(fixtures_dir / "comment_preamble.tsv"), "--emit-code"])
    assert result.exit_code == 0
    assert "import pandas as pd" in result.output
    assert "df.rename" in result.output


def test_scan_a_headerless_file_still_reports(fixtures_dir) -> None:
    result = runner.invoke(app, ["scan", str(fixtures_dir / "headerless.tsv")])
    # A verdict is still produced, but the low header confidence is visible.
    assert "confidence" in result.output.lower()


def test_extract_writes_a_clean_table(fixtures_dir, tmp_path) -> None:
    out = tmp_path / "clean.tsv"
    result = runner.invoke(
        app,
        [
            "extract",
            str(fixtures_dir / "ssf_like.tsv.gz"),
            "--output",
            str(out),
            "--max-rows",
            "20",
        ],
    )
    assert result.exit_code == 0
    assert out.exists()
    assert "Transformations applied" in result.output
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 21  # header plus 20 rows


def test_extract_report_json(fixtures_dir, tmp_path) -> None:
    out = tmp_path / "clean.tsv"
    report = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "extract",
            str(fixtures_dir / "comment_preamble.tsv"),
            "--output",
            str(out),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert payload["succeeded"]
    assert payload["header_row_index"] == 2
    assert payload["normalization"]["declined"]


def test_extract_missing_file_exits_nonzero(tmp_path) -> None:
    result = runner.invoke(app, ["extract", str(tmp_path / "nope.tsv")])
    assert result.exit_code == 1
    assert "Extraction failed" in result.output


def test_benchmark_on_the_shipped_template() -> None:
    from pathlib import Path

    template = Path(__file__).parent.parent / "benchmark" / "benchmark_manifest_template.csv"
    assert template.exists(), f"the shipped manifest template is missing: {template}"
    result = runner.invoke(app, ["benchmark", str(template)])
    assert result.exit_code == 0
    # The template ships with no ground truth, and the tool must say so rather
    # than report metrics computed from nothing.
    assert "ground truth" in result.output


def test_benchmark_writes_metrics_json(tmp_path) -> None:
    import pandas as pd

    from gwaspoker.benchmark.manifest import MANIFEST_COLUMNS

    manifest = tmp_path / "manifest.csv"
    row = dict.fromkeys(MANIFEST_COLUMNS, "")
    row.update(
        {
            "study_accession": "GCST1",
            "predicted_header": "CHR\tPOS",
            "predicted_prs_ready": "READY",
            "ground_truth_header": "CHR\tPOS\tSNP",
            "ground_truth_prs_ready": "READY",
            "probe_bytes": "262144",
            "full_file_size": "1000000",
        }
    )
    pd.DataFrame([row], columns=list(MANIFEST_COLUMNS)).to_csv(manifest, index=False)

    out = tmp_path / "metrics.json"
    result = runner.invoke(app, ["benchmark", str(manifest), "--output", str(out)])
    assert result.exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))["results"]
    assert payload["rows_with_ground_truth"] == 1
    assert payload["header_detection"]["exact_ordered_header_match"]["rate"] == 0.0


def test_benchmark_missing_manifest_exits_nonzero(tmp_path) -> None:
    result = runner.invoke(app, ["benchmark", str(tmp_path / "absent.csv")])
    assert result.exit_code == 1


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


def test_defaults() -> None:
    config = GWASPokerConfig()
    assert config.probe_bytes == DEFAULT_PROBE_BYTES == 262_144
    assert config.request_timeout == 60.0
    assert config.prefer_harmonised == "auto"
    assert config.enable_llm_fallback is False


def test_environment_overrides_defaults() -> None:
    config = load_config(env={"GWASPOKER_PROBE_BYTES": "65536"})
    assert config.probe_bytes == 65536


def test_environment_parses_booleans_and_floats() -> None:
    config = load_config(
        env={"GWASPOKER_ENABLE_LLM_FALLBACK": "true", "GWASPOKER_REQUEST_TIMEOUT": "12.5"}
    )
    assert config.enable_llm_fallback is True
    assert config.request_timeout == 12.5


def test_explicit_overrides_beat_the_environment() -> None:
    config = load_config(env={"GWASPOKER_PROBE_BYTES": "65536"}, probe_bytes=1_048_576)
    assert config.probe_bytes == 1_048_576


def test_yaml_config_file(tmp_path) -> None:
    path = tmp_path / "gwaspoker.yaml"
    path.write_text("probe_bytes: 131072\nprefer_harmonised: 'no'\n", encoding="utf-8")
    config = load_config(path, env={})
    assert config.probe_bytes == 131072
    assert config.prefer_harmonised == "no"


def test_config_file_under_a_gwaspoker_table(tmp_path) -> None:
    path = tmp_path / "gwaspoker.yaml"
    path.write_text("gwaspoker:\n  probe_bytes: 524288\n", encoding="utf-8")
    assert load_config(path, env={}).probe_bytes == 524288


def test_unknown_config_keys_are_ignored_with_a_warning(tmp_path, caplog) -> None:
    path = tmp_path / "gwaspoker.yaml"
    path.write_text("probe_bytes: 131072\nnot_a_setting: 5\n", encoding="utf-8")
    config = load_config(path, env={})
    assert config.probe_bytes == 131072


def test_missing_config_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "absent.yaml")


def test_with_overrides_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        GWASPokerConfig().with_overrides(not_a_field=1)


def test_config_serialises_paths_as_strings() -> None:
    payload = GWASPokerConfig().to_dict()
    assert isinstance(payload["download_dir"], str)
    json.dumps(payload)


def test_probe_size_ladder_matches_the_documented_options() -> None:
    from gwaspoker.config import PROBE_SIZE_LADDER

    assert PROBE_SIZE_LADDER == (65_536, 131_072, 262_144, 524_288, 1_048_576)


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def test_html_report_is_self_contained(tmp_path, fixtures_dir) -> None:
    """v1 linked Bootstrap from a CDN, so the report was unstyled offline."""
    from gwaspoker.reporting.html import write_report

    path = write_report(tmp_path / "report.html", title="Test report")
    html = path.read_text(encoding="utf-8")
    assert "<style>" in html
    assert "maxcdn.bootstrapcdn.com" not in html
    assert "http://" not in html.split("<style>")[0]


def test_html_report_renders_an_assessment(tmp_path) -> None:
    from gwaspoker.catalog.discovery import AssessmentResult
    from gwaspoker.catalog.models import Study
    from gwaspoker.readiness.prs import assess_from_declared_fields
    from gwaspoker.reporting.html import write_report

    result = AssessmentResult(
        target="GCST1",
        study=Study(study_accession="GCST1", reported_trait="Migraine"),
        readiness=assess_from_declared_fields(
            ("chromosome", "base_pair_location", "effect_allele", "other_allele", "beta", "p_value")
        ),
    )
    path = write_report(tmp_path / "report.html", assessments=[result])
    html = path.read_text(encoding="utf-8")
    assert "GCST1" in html
    assert "READY" in html
    assert "Migraine" in html


def test_json_report_carries_provenance(tmp_path, config) -> None:
    from gwaspoker.provenance import build_provenance
    from gwaspoker.reporting.json import write_json

    record = build_provenance(config, command="gwaspoker test")
    path = write_json({"x": 1}, tmp_path / "out.json", provenance=record, kind="test")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["report_type"] == "test"
    assert payload["provenance"]["environment"]["gwaspoker_version"] == __version__
    assert payload["provenance"]["environment"]["python_version"]
    assert payload["provenance"]["configuration"]["probe_bytes"] == DEFAULT_PROBE_BYTES


# ----------------------------------------------------------------------
# Search availability columns
# ----------------------------------------------------------------------


def test_search_csv_carries_the_availability_columns(tmp_path) -> None:
    from gwaspoker.catalog.discovery import SearchResult
    from gwaspoker.catalog.models import Study
    from gwaspoker.reporting.csv import SEARCH_COLUMNS, write_search_csv

    for column in ("file_available", "metadata_available", "harmonised_available", "ssf_status"):
        assert column in SEARCH_COLUMNS

    result = SearchResult(
        study=Study(study_accession="GCST1", reported_trait="Migraine"),
        file_available=True,
        metadata_available=True,
        harmonised_available=False,
        ssf_status="GWAS-SSF",
    )
    path = write_search_csv([result], tmp_path / "search.csv")

    import pandas as pd

    frame = pd.read_csv(path)
    assert bool(frame["file_available"].iloc[0]) is True
    assert bool(frame["harmonised_available"].iloc[0]) is False
    assert frame["ssf_status"].iloc[0] == "GWAS-SSF"


def test_search_table_renders_yes_no_question(capsys) -> None:
    """The four columns must read yes / no / ? -- never a blank."""
    from gwaspoker.catalog.discovery import SearchResult
    from gwaspoker.catalog.models import Study
    from gwaspoker.reporting.console import console, render_search_results

    results = [
        SearchResult(
            study=Study(study_accession="GCST1", reported_trait="Migraine"),
            file_available=True,
            metadata_available=True,
            harmonised_available=True,
            ssf_status="GWAS-SSF",
        ),
        SearchResult(
            study=Study(study_accession="GCST2", reported_trait="Migraine"),
            file_available=True,
            metadata_available=False,
            harmonised_available=False,
            ssf_status=None,
        ),
    ]
    with console.capture() as capture:
        render_search_results(results, trait="migraine")
    output = capture.get()

    for header in ("File", "API", "Harmonised", "GWAS-SSF"):
        assert header in output
    assert "?" in output  # GCST2's unestablished SSF status
    assert "Sum stats" not in output  # the column it replaced


def test_tristate_distinguishes_unknown_from_false() -> None:
    from gwaspoker.reporting.console import _tristate

    assert _tristate(True).plain == "yes"
    assert _tristate(False).plain == "no"
    assert _tristate(None).plain == "?"


def test_search_help_documents_the_check_files_flag() -> None:
    result = runner.invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    assert "--no-check-files" in result.stdout
