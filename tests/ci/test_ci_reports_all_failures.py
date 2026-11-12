"""#910: CI reports every failure, runs on branch pushes and never cancels main."""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import ci  # noqa: E402
import ci_summary  # noqa: E402

JUNIT = """<testsuites><testsuite name="pytest" tests="3" time="12.0">
<testcase classname="tests.a.test_x" name="test_ok" time="1.0"/>
<testcase classname="tests.a.test_x" name="test_bad_one" time="2.0"><failure message="m"/></testcase>
<testcase classname="tests.b.test_y" name="test_bad_two" time="9.0"><error message="m"/></testcase>
</testsuite></testsuites>"""


def _quality() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text("utf-8"))


def test_pytest_is_not_stopped_at_the_first_failure() -> None:
    addopts = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"][
        "pytest"
    ]["ini_options"]["addopts"]
    assert not [a for a in addopts if "maxfail" in a]
    command = next(
        s.command for s in ci.plan("quality") if "tools/stall_watchdog.py" in s.command
    )
    assert "maxfail" not in command


def _failing_section(text: str) -> str:
    return text.split("### Failing tests")[1].split("### Slowest")[0]


def test_summary_lists_every_failing_test_and_timing(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    # 12 passing tests slower than the failures: the failures are outside the slowest ten.
    slow = "".join(
        f'<testcase classname="tests.c.test_z" name="test_slow{i}" time="50.0"/>'
        for i in range(12)
    )
    junit.write_text(JUNIT.replace("</testsuite>", slow + "</testsuite>"), "utf-8")
    text = ci_summary.render([junit])
    assert "failing: 2" in text
    section = _failing_section(text)
    assert "tests.a.test_x::test_bad_one" in section
    assert "tests.b.test_y::test_bad_two" in section
    assert "test_bad_one" not in text.split("### Slowest")[1]
    assert "pytest wall time" in text


def test_summary_without_junit_says_there_is_no_verdict(tmp_path: Path) -> None:
    assert "no verdict" in ci_summary.render([tmp_path / "missing.xml"])


def test_interrupted_run_is_labelled_partial(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text(
        '<testsuite time="1"><testcase classname="a.b" name="ok" time="1"/>'
        '<testcase name="interrupted" time="0"/></testsuite>',
        "utf-8",
    )
    assert "PARTIAL" in ci_summary.render([junit])


def test_summary_merges_both_junit_files(tmp_path: Path) -> None:
    one, two = tmp_path / "1.xml", tmp_path / "2.xml"
    one.write_text(
        '<testsuite time="1"><testcase classname="a" name="x"/></testsuite>', "utf-8"
    )
    two.write_text(
        '<testsuite time="1"><testcase classname="app" name="y"><failure/></testcase>'
        "</testsuite>",
        "utf-8",
    )
    text = ci_summary.render([one, two])
    assert "tests: 2, failing: 1" in text and "app::y" in _failing_section(text)


def test_quality_writes_junit_and_publishes_summary_even_on_failure() -> None:
    steps = _quality()["jobs"]["quality"]["steps"]
    pytest_step = next(s for s in steps if "--junitxml" in s.get("run", ""))
    summary = next(s for s in steps if "ci_summary.py --junit" in s.get("run", ""))
    schwab = next(s for s in steps if "apps/schwab-trading/tests" in s.get("run", ""))
    assert summary["if"] == "always()"
    assert "always()" in schwab["if"] and "--junitxml" in schwab["run"]
    assert steps.index(summary) > steps.index(schwab) > steps.index(pytest_step)
    assert 'junitxml="${{ runner.temp }}' in pytest_step["run"]  # quoted: backslashes


def test_branch_pushes_run_and_main_is_never_cancelled() -> None:
    text = (ROOT / ".github/workflows/quality.yml").read_text("utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)  # `on` stays a string key
    triggers = document["on"]
    assert "push" in triggers and triggers["push"] == ""  # empty: no branch filter
    concurrency = document["concurrency"]
    assert "github.event_name" in concurrency["group"]
    assert "github.run_id" in concurrency["group"]  # main runs never share a slot
    assert concurrency["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"


def test_local_runner_reports_all_failed_steps(monkeypatch, capsys) -> None:
    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code

    calls: list[str] = []

    def fake_run(argv, **_kw):
        calls.append(argv[0])
        return Done(1 if argv[0].endswith(("one", "two")) else 0)

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ci, "_argv", lambda command: [command])
    steps = [ci.Step(n, n, {}) for n in ("one", "two", "three")]
    assert ci.run(steps) == 1
    assert calls == ["one", "two", "three"]  # old code stopped after "one"
    err = capsys.readouterr().err
    assert "FAILED (1): one" in err and "FAILED (1): two" in err


def test_hosted_env_step_is_not_planned_locally() -> None:
    assert not [s for s in ci.plan("quality") if "QE_JOB_START" in s.command]
