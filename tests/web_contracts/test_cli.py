from __future__ import annotations

import importlib
from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import quant_engine.web_contracts as web_contracts
from quant_engine.web_contracts import extract_har
from quant_engine.web_contracts import evidence


def _cli_module():  # type: ignore[no-untyped-def]
    try:
        return importlib.import_module("quant_engine.web_contracts.cli")
    except ModuleNotFoundError:
        pytest.fail("the generalized web-contract CLI module is missing")


def _join(*parts: str) -> str:
    return "".join(parts)


def _complete_source() -> tuple[bytes, str]:
    sentinel = _join("complete", "-private-value")
    entry = {
        "request": {
            "method": "POST",
            "url": "https://example.invalid/complete",
            "headers": [
                {"name": _join("Author", "ization"), "value": f"Bearer {sentinel}"}
            ],
            "cookies": [{"name": "session", "value": sentinel}],
            "postData": {"text": sentinel},
        },
        "response": {
            "status": 418,
            "headers": [{"name": "Set-Cookie", "value": f"session={sentinel}"}],
            "cookies": [{"name": "session", "value": sentinel, "httpOnly": True}],
            "content": {"text": sentinel},
        },
        "unknown": {"complete_key": sentinel},
    }
    document = {_join("l", "og"): {_join("entr", "ies"): [entry]}}
    return json.dumps(document, separators=(",", ":")).encode(), sentinel


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_distribution_registers_the_web_contract_command() -> None:
    commands = {
        entry.name: entry.value
        for entry in metadata.distribution(
            "quant-engine-web-contract-harvester"
        ).entry_points
    }

    assert commands["quant-engine-web-contract"] == (
        "quant_engine.web_contracts.cli:main"
    )


def test_package_and_distribution_versions_match() -> None:
    assert web_contracts.__version__ == metadata.version(
        "quant-engine-web-contract-harvester"
    )


def test_version_reports_the_cli_surface_release(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli_module()

    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    output = capsys.readouterr()
    assert output.out == "quant-engine-web-contract 0.3.0\n"
    assert output.err == ""


def test_help_works_from_source_without_installed_distribution_metadata() -> None:
    source_root = (
        Path(__file__).resolve().parents[2] / "packages" / "web-contracts" / "src"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    command = (
        "from quant_engine.web_contracts.cli import main; "
        "raise SystemExit(main(['--help']))"
    )

    result = subprocess.run(
        [sys.executable, "-S", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert "{extract}" in result.stdout
    assert result.stderr == ""


def test_help_exposes_only_the_accepted_offline_extract_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _cli_module()

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr()
    assert "quant-engine-web-contract" in output.out
    assert "extract" in output.out
    assert "capture" not in output.out
    assert "verify" not in output.out
    assert output.err == ""


def test_extract_matches_direct_api_and_never_prints_captured_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()
    source, sentinel = _complete_source()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(source)
    cli_output = tmp_path / "cli-output"
    api_output = tmp_path / "api-output"

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(source_path),
            "--output-dir",
            str(cli_output),
        ]
    )
    extract_har(source_path, api_output)

    assert exit_code == 0
    assert _tree(cli_output) == _tree(api_output)
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "materialized_entry_count": 1,
        "ok": True,
        "parse_status": "COMPLETE",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_size_bytes": len(source),
    }
    assert sentinel not in output.out
    assert sentinel not in output.err


def test_installed_launcher_preserves_values_and_stream_contract(
    tmp_path: Path,
) -> None:
    source, sentinel = _complete_source()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(source)
    output_dir = tmp_path / "launcher-output"
    executable = Path(sys.executable).with_name(
        "quant-engine-web-contract.exe"
        if os.name == "nt"
        else "quant-engine-web-contract"
    )

    result = subprocess.run(
        [
            str(executable),
            "extract",
            "--har-file",
            str(source_path),
            "--output-dir",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["parse_status"] == "COMPLETE"
    assert result.stderr == ""
    assert (output_dir / "source.har").read_bytes() == source
    assert (
        sentinel.encode()
        in (output_dir / "entries" / "000000" / "entry.json").read_bytes()
    )
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_invalid_input_is_preserved_and_returns_noncomplete_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()
    sentinel = _join("unclosed", "-private-value")
    source = ('{"value":"' + sentinel).encode()
    source_path = tmp_path / "invalid.har"
    source_path.write_bytes(source)
    output_dir = tmp_path / "invalid-output"

    exit_code = cli.main(
        ["extract", "--input", str(source_path), "--output-dir", str(output_dir)]
    )

    assert exit_code == 2
    assert (output_dir / "source.har").read_bytes() == source
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "materialized_entry_count": 0,
        "ok": False,
        "parse_status": "INVALID_JSON",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_size_bytes": len(source),
    }
    assert sentinel not in output.out
    assert sentinel not in output.err


def test_partial_input_is_preserved_and_returns_noncomplete_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()
    sentinel = _join("partial", "-private-value")
    document = {
        _join("l", "og"): {
            _join("entr", "ies"): [
                {
                    "response": {
                        "content": {
                            "text": f"not-base64-{sentinel}",
                            "encoding": "base64",
                        }
                    }
                }
            ]
        }
    }
    source = json.dumps(document, separators=(",", ":")).encode()
    source_path = tmp_path / "partial.har"
    source_path.write_bytes(source)
    output_dir = tmp_path / "partial-output"

    exit_code = cli.main(
        ["extract", "--har-file", str(source_path), "--output-dir", str(output_dir)]
    )

    assert exit_code == 2
    assert (output_dir / "source.har").read_bytes() == source
    output = capsys.readouterr()
    assert json.loads(output.out)["parse_status"] == "PARTIAL"
    assert output.err == ""
    assert sentinel not in output.out


@pytest.mark.parametrize(
    "legacy_arguments",
    [
        ["--filter-domain", "legacy-private-value"],
        ["--extract-html"],
        ["--site-name", "legacy-private-value"],
    ],
)
def test_rejected_legacy_arguments_do_not_echo_argument_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    legacy_arguments: list[str],
) -> None:
    cli = _cli_module()
    sentinel = _join("legacy", "-private-value")

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(tmp_path / "input.har"),
            "--output-dir",
            str(tmp_path / "output"),
            *legacy_arguments,
        ]
    )

    assert exit_code == 64
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "CLI_USAGE", "ok": False}
    assert sentinel not in output.err


def test_extract_requires_an_explicit_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()

    exit_code = cli.main(["extract", "--output-dir", str(tmp_path / "output")])

    assert exit_code == 64
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "CLI_USAGE", "ok": False}


@pytest.mark.parametrize("command", ["capture", "verify"])
def test_unavailable_command_names_are_rejected_without_argument_echo(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()

    exit_code = cli.main([command, _join("command", "-private-value")])

    assert exit_code == 64
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "CLI_USAGE", "ok": False}


def test_missing_source_reports_only_a_value_safe_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()
    sentinel = _join("missing", "-private-value")
    output_dir = tmp_path / "output"

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(tmp_path / f"{sentinel}.har"),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 1
    assert not output_dir.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "SOURCE_READ_FAILED", "ok": False}
    assert sentinel not in output.err


def test_output_side_file_not_found_is_classified_as_io_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _cli_module()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(b"{}")

    def fail_output(source: Path, output_dir: Path) -> None:
        del source, output_dir
        raise FileNotFoundError

    monkeypatch.setattr(cli, "extract_har", fail_output)

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(source_path),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "IO_ERROR", "ok": False}


def test_existing_output_is_not_modified_and_its_path_is_not_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _cli_module()
    source, _ = _complete_source()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(source)
    sentinel = _join("existing", "-private-value")
    output_dir = tmp_path / sentinel
    output_dir.mkdir()
    marker = output_dir / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(source_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 1
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in output_dir.iterdir()) == ["marker.txt"]
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "OUTPUT_EXISTS", "ok": False}
    assert sentinel not in output.err


def test_interruption_keeps_source_and_checkpoint_without_echoing_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _cli_module()
    source, sentinel = _complete_source()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(source)
    output_dir = tmp_path / "interrupted"
    original_write = evidence._write_flushed

    def interrupt_first_entry(path: Path, value: bytes) -> None:
        if path.name == "entry.json":
            raise KeyboardInterrupt
        original_write(path, value)

    monkeypatch.setattr(evidence, "_write_flushed", interrupt_first_entry)

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(source_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 130
    assert (output_dir / "source.har").read_bytes() == source
    manifest = json.loads((output_dir / "manifest.json").read_bytes())
    assert manifest["parse_status"] == "PARSED"
    assert manifest["completeness"] == "DERIVATIVES_NOT_FINALIZED"
    assert manifest["source_entry_count"] == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "INTERRUPTED", "ok": False}
    assert sentinel not in output.err


def test_interruption_during_argument_parsing_is_value_safe(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _cli_module()

    def interrupt_parser() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_parser", interrupt_parser)

    exit_code = cli.main([])

    assert exit_code == 130
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "INTERRUPTED", "ok": False}


def test_interruption_during_receipt_output_is_value_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _cli_module()
    source, sentinel = _complete_source()
    source_path = tmp_path / "input.har"
    source_path.write_bytes(source)
    output_dir = tmp_path / "output"
    original_json_line = cli._json_line

    def interrupt_receipt(value: object) -> str:
        if isinstance(value, dict) and "parse_status" in value:
            raise KeyboardInterrupt
        return original_json_line(value)

    monkeypatch.setattr(cli, "_json_line", interrupt_receipt)

    exit_code = cli.main(
        ["extract", "--har-file", str(source_path), "--output-dir", str(output_dir)]
    )

    assert exit_code == 130
    assert (output_dir / "source.har").read_bytes() == source
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "INTERRUPTED", "ok": False}
    assert sentinel not in output.err


def test_unexpected_exception_text_is_not_echoed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _cli_module()
    sentinel = _join("exception", "-private-value")

    def fail_extract(source: Path, output_dir: Path) -> None:
        del source, output_dir
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "extract_har", fail_extract)

    exit_code = cli.main(
        [
            "extract",
            "--har-file",
            str(tmp_path / "input.har"),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "INTERNAL_ERROR", "ok": False}
    assert sentinel not in output.err
