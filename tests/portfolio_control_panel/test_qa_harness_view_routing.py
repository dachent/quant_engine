"""Source pins for the browser QA scripts' view routing.

The FV-5 full verification could only pass its browser QA with patched copies of
``browser_qa.mjs`` and ``interaction_qa.mjs``: a leverage check read Capital-view
elements on every capture, and the account-manifest checks ran while a different view
was active.  These pins fail if either routing fix is lost.
"""

import re
from pathlib import Path

PANEL_APP = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"


def _read(name: str) -> str:
    return (PANEL_APP / name).read_text(encoding="utf-8")


def test_browser_qa_scopes_leverage_dom_check_to_capital_view() -> None:
    source = _read("browser_qa.mjs")
    guard = source.index('if (capture.view === "capital") {')
    status_read = source.index('page.locator("#leverage-status")')
    rendered_error = source.index(
        "rendered leverage disclosures diverge from the bound panel payload"
    )
    absent_error = source.index(
        "absent leverage disclosures must render as not evaluated, never as zero"
    )
    screenshot = source.index(
        'await page.screenshot({ path: path.join(folder, "screenshot.png")'
    )

    assert source.count('page.locator("#leverage-status")') == 1
    assert guard < status_read < absent_error < rendered_error < screenshot, (
        "leverage DOM read and both of its assertions must sit inside the Capital guard"
    )
    # The guard must close after the last leverage assertion and before the screenshot,
    # i.e. the assertions are scoped, not removed.
    between = source[rendered_error:screenshot]
    assert between.count("}") >= 3, (
        "Capital guard is not closed around the leverage check"
    )


def test_browser_qa_keeps_the_long_page_timeout_budget() -> None:
    source = _read("browser_qa.mjs")
    match = re.search(r"const QA_PAGE_TIMEOUT_MS = (\d+);", source)
    assert match and int(match.group(1)) >= 300000
    assert "page.setDefaultTimeout(QA_PAGE_TIMEOUT_MS);" in source
    assert 'waitUntil: "load", timeout: QA_PAGE_TIMEOUT_MS' in source
    assert "timeout: 30000" not in source


def test_interaction_qa_routes_to_risk_before_account_manifest_checks() -> None:
    source = _read("interaction_qa.mjs")
    navigation = source.index("  await page.locator('[data-nav=\"risk\"]').click();")
    assert (
        "if (await page.locator('[data-nav=\"risk\"]').count()) {"
        in source[navigation - 120 : navigation]
    )
    epoch_table = source.index('page.locator("#account-manifest-table")')
    epoch_authority = source.index('page.locator("#account-manifest-authority")')
    epoch_legend = source.index('page.locator("#account-manifest-legend")')
    first_epoch_check = source.index("epochHeaders")

    assert navigation < epoch_table < epoch_authority < epoch_legend
    assert navigation < first_epoch_check
    # Nothing between the navigation and the first manifest read may re-route away.
    assert "data-nav" not in source[navigation + 60 : epoch_table]
    # No earlier read of the manifest DOM may precede the navigation.
    assert source.index('locator("#account-manifest-') > navigation
