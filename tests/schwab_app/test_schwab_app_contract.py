from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPOSITORY_ROOT / "apps" / "schwab-trading"


def _source(name: str) -> str:
    return (APP_ROOT / name).read_text(encoding="utf-8")


def test_source_history_and_runtime_boundary_are_explicit() -> None:
    provenance = _source("PROVENANCE.md")
    storage = _source("storage.py")

    assert "4864bc1428563b79cdeda35d55dd5f7c73b4ee3c" in provenance
    assert "without squashing" in provenance
    assert 'RUNTIME_ROOT_ENV = "QUANT_ENGINE_SCHWAB_HOME"' in storage
    assert 'repository_root.parent / "dataSchwab" / "runtime"' in storage
    assert "Path(__file__).resolve().parent\n" not in storage


def test_every_retained_mutation_surface_has_an_unconditional_guard() -> None:
    guard = _source("execution_guard.py")
    tasks = _source("tasks.py")
    client = _source("schwab_client.py")
    runner = _source("runner.py")
    ui = _source("ui.py")

    assert "getenv" not in guard and "environ" not in guard
    assert tasks.index('require_submission_authority("place_orders")') < tasks.index(
        "profile = _load_profile(args)", tasks.index("def task_place_orders")
    )
    assert client.index('require_submission_authority("place_order")') < client.index(
        "return self.ensure_client().place_order"
    )
    assert client.index('require_submission_authority("cancel_order")') < client.index(
        "return self.ensure_client().cancel_order"
    )
    runner_guard = runner.index('request.task_name == "place_orders"')
    assert runner_guard < runner.index("init_db()", runner_guard)
    assert '"Place Orders (disabled)"' in ui
    assert 'button.state(["disabled"])' in ui
