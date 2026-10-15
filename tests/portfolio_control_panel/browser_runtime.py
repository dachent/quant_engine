"""Project-local Node and Playwright runtime resolution for browser tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import NamedTuple


class BrowserRuntime(NamedTuple):
    """Resolved browser-test inputs, all owned by the project or explicit overrides."""

    node: str
    playwright_module: str
    executable: str


_DISCOVER_EXECUTABLE = """
import { pathToFileURL } from \"node:url\";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
process.stdout.write(chromium.executablePath());
"""


def resolve_browser_runtime(repository_root: Path) -> BrowserRuntime | None:
    """Return the project-local browser runtime, or ``None`` when it is unavailable."""

    node = shutil.which("node")
    if node is None:
        return None

    configured_module = os.environ.get("PLAYWRIGHT_MODULE")
    playwright_module = (
        Path(configured_module)
        if configured_module
        else repository_root
        / "apps"
        / "portfolio-control-panel"
        / "node_modules"
        / "playwright"
        / "index.mjs"
    )
    if not playwright_module.is_file():
        return None

    configured_executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
    if configured_executable:
        executable = Path(configured_executable)
    else:
        environment = dict(os.environ)
        environment["PLAYWRIGHT_MODULE"] = str(playwright_module)
        try:
            probe = subprocess.run(
                [node, "--input-type=module", "-e", _DISCOVER_EXECUTABLE],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=60,  # a loaded hosted runner needs >10s to import playwright
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if probe.returncode != 0 or not probe.stdout.strip():
            return None
        executable = Path(probe.stdout.strip())

    if not executable.is_file():
        return None
    return BrowserRuntime(node, str(playwright_module), str(executable))


def require_browser_runtime(repository_root: Path) -> BrowserRuntime:
    """Return the project runtime or fail mandatory rendered-browser tests clearly."""

    runtime = resolve_browser_runtime(repository_root)
    assert runtime is not None, (
        "the project-local Node, Playwright module, and Chromium runtime are required"
    )
    return runtime
