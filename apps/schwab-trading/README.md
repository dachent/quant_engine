# Schwab Trading Engine for `quant_engine`

Windows operator application absorbed from the original Excel/VBA Schwab
workflow. The existing Tkinter UI and request/result runner remain available,
but order submission and cancellation are unconditionally disabled in this
release.

Source history and the project-specific delta are recorded in
[`PROVENANCE.md`](PROVENANCE.md).

## Run

```powershell
$env:QUANT_ENGINE_SCHWAB_HOME = "C:\path\to\_quant_engine\dataSchwab\runtime"
uv run --frozen --project apps/schwab-trading python apps/schwab-trading/ui.py
```

If `QUANT_ENGINE_SCHWAB_HOME` is omitted in this monorepo, the app resolves the
sibling `dataSchwab\runtime` folder. Runtime jobs, results, logs, imports,
exports, SQLite state, and DPAPI credentials never belong in the worktree.

## Callable task runner

The UI and command invocation use the same task registry:

```powershell
uv run --frozen --project apps/schwab-trading python apps/schwab-trading/runner.py `
  --task refresh_portfolio `
  --request-file C:\path\to\request.json `
  --result-file C:\path\to\result.json
```

Read-side tasks still require an authorized local Schwab session. The retained
`place_orders` task returns a structured `SubmissionDisabledError` before
client construction, quotes, SQLite mutation, or broker access. The UI control
is visibly disabled. There is no configuration or environment bypass.

## Auth Diagnostics

Run the isolated auth helper when you want to debug login without exercising the rest of the app:

```powershell
& ".\.venv\Scripts\python.exe" ".\auth_diagnostic.py" manual --force-refresh
& ".\.venv\Scripts\python.exe" ".\auth_diagnostic.py" auto --force-refresh
```

- `manual` prints the generated Schwab authorization URL, then asks you to paste the final redirect URL after login.
- `auto` exercises the normal loopback callback capture path.
- `--browser <name>` optionally passes a browser override through to the loopback login flow; the default remains your installed browser.

The production login path should continue to use the installed browser by default. A real browser is a better fit than Playwright here because Schwab's OAuth flow depends on normal user-driven login, 2FA, trust prompts, and accepting the loopback certificate warning.

## First-use checklist

1. Update the Schwab app callback URI to an explicit loopback URL such as `https://127.0.0.1:8182`.
2. Open the app.
3. Enter `App Key`, `App Secret`, and `Callback URI`.
4. Click `Login / Refresh Auth`.
5. Use the `Files` block to choose an `Import File` and, optionally, an `Export Folder`.
6. Use `imports\order_template.xlsx` as the starting import template.
7. If your browser shows a certificate warning for the local callback URL, approve it so the loopback redirect can complete.

The callback URL must exactly match the Schwab developer app entry, including whether it omits a trailing slash. The current app expects the no-trailing-slash form, for example `https://127.0.0.1:8182`.

## Layout

- `ui.py`: Tkinter desktop shell
- `runner.py`: subprocess task executor
- `tasks.py`: application tasks
- `schwab_client.py`: `schwab-py` wrapper + encrypted token handling
- `order_builders.py`: order-spec construction
- `pricing.py`: legacy and baseline NBBO pricing
- `imports.py`: import parsing + template generation
- `storage.py`: DPAPI secret storage + SQLite state
- `logs\`: UI, runner, and audit logs
- `state\app.db`: local snapshots and placed-order state
- `state\credentials.json.dpapi`: encrypted credentials and token data

## Workflow

- The top action bar follows the trading workflow: `Login / Refresh Auth`, `Refresh Accounts`, `Refresh Portfolio`, `Refresh Orders`, `Open Import Template`, `Import`, `Validate`, `Refresh Quotes`, `Place Orders`, `Export`.
- The left rail includes a `Files` block with separate `Import File` and `Export Folder` controls.
- `Import` opens a workbook picker and fills the `Import File` field.
- `Open Import Template` creates the template workbook if needed and points the import field at it.
- `Export` writes a timestamped workbook into the selected export folder. If `Export Folder` is blank, the app falls back to the default `exports\` directory.
- The main window title is `Schwab Trading Engine`, and the notebook order follows the workflow-oriented data sequence: `Accounts`, `Portfolio`, `Order Status`, `Orders Preview`, `Logs`.

## Notes

- `Validate` is structural validation only; it may mark rows as `NEEDS_QUOTES` when quote-derived limit pricing is required.
- `Refresh Quotes` computes preview pricing and execution order.
- `Place Orders (disabled)` is retained only to show the legacy workflow
  boundary; it cannot submit.
- `Refresh Orders` is the broker-source-of-truth status view and backfills rejection details plus tax-lot data when needed.

## Tests

```powershell
uv run --frozen --project apps/schwab-trading pytest -q `
  --disable-socket --allow-hosts=127.0.0.1,localhost `
  apps/schwab-trading/tests
```
