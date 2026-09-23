import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(r"C:\Dev\worktrees\quant-engine-completion\.pt\d414")
sys.path[:0] = [
    str(root),
    str(root / "tools"),
    *(str(path) for path in (root / "packages").glob("*/src")),
]
import portfolio_pipeline_materialize as materializer
import portfolio_release_builder as release

release._validated_reusable_provider_ledger(
    Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\source\current-native-release-v13\proof\stages\ledger\generations\fd1ae097a02c469bb2ac03443d07d8e11787110ce6c0b6983be0dd3a3053f404"
    )
)
real_run = subprocess.run


def boundary(command: object, *args: object, **kwargs: object) -> object:
    values = [str(value) for value in command]
    allowed = (
        values == ["ver"]
        or Path(values[0]).stem.lower() in ("git", "git.cmd", "git.exe")
        or (
            "-I" in values
            and "-c" in values
            and not any("portfolio_" in value for value in values)
        )
    )
    print(f"allowed={allowed} command={values!r}", flush=True)
    assert allowed, values
    return real_run(values, *args, **kwargs)


args = argparse.Namespace(
    repo_root=root,
    python=Path(sys.executable),
    workspace_root=Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\d414\.scratch\e-90a0ac68\resume-exam\workspace"
    ),
    output_root=Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\d414\.scratch\e-90a0ac68\resume-exam\diagnostic-output"
    ),
    retained_cache_root=Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\d414\.scratch\e-90a0ac68\resume-exam\cache"
    ),
    requested_as_of="2026-09-19",
    source_run=Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\d414\.scratch\e-90a0ac68\f\source-run.json"
    ),
    workbook_runtime=Path(
        r"C:\Dev\worktrees\quant-engine-completion\.pt\source\workbook-runtime\runtime.json"
    ),
    hwm_governed_result=None,
    hwm_governed_input_generation=None,
    hwm_governed_calendar_authority=None,
    hwm_governed_admission_receipt=None,
    invested_capital_result=None,
    invested_capital_input_generation=None,
    qa_dir=None,
)
with patch.object(materializer.subprocess, "run", side_effect=boundary):
    materializer._attended_preflight(args)
print("PREFLIGHT_OK")
