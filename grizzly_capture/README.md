# grizzly_capture

Deployment-local authenticated capture driver for the GrizzlyBulls expanded
site's session-bearing surfaces: the model trade table (newest trades) and
the current-positions surface. It mirrors the accepted `schwab_capture`
pattern: a supervised CDP-attached browser, owner login inside the driven
window (the agent never touches credentials), persist-then-parse artifacts,
a create-only run directory, and a counts/hashes-only manifest.

## Supervised-run procedure

1. The owner launches their real browser with CDP enabled, e.g. port 9223:

   ```
   chrome --remote-debugging-port=9223
   ```

2. The owner logs in to the site inside that browser window. The agent never
   sees, types, or stores credentials.

3. The agent runs the driver against the already-authenticated session:

   ```
   python -m grizzly_capture.cli \
       --run-dir <new-run-dir> \
       --cdp-port 9223 \
       --model-slug vix-ta-macro-advanced
   ```

   Optional overrides: `--route {auto,download,state}`, `--base-url`,
   `--download-timeout`.

The driver navigates the attached tab to `/models/<slug>`, verifies an
authenticated marker, creates the run directory (create-only: an existing
directory fails the run), captures trades, captures the positions table, and
finalizes `manifest.json`.

## Trades routes

Attempted in order under `--route auto`; each route is byte-evidenced in the
manifest (`artifacts[]` carries path, SHA-256, size, row count, and source).

- **DOWNLOAD_ROUTE (`download`)** — sets CDP `Browser.setDownloadBehavior`
  (allow, `downloadPath = <run-dir>/downloads`), switches Chart -> Table
  view, walks the rendered DOM for the CSV control (the control appears only
  in certain view states), clicks it, and waits for the client-generated
  `blob:` download to land. The artifact is accepted only when its header is
  exactly `Date (ET), Action, SPX, Prev Price, P/L, PnL %, Model, Buy & Hold`.
  `trades.csv` is the canonical byte copy of the provider's own CSV.
- **STATE_ROUTE (`state`)** — reads the trades array from the page's React
  state (a fixed, read-only fiber-walk script anchored on the rendered table;
  the driver never evaluates arbitrary JavaScript) and serializes it to the
  same CSV column shape. The manifest notes that the artifact is
  state-derived, not the provider's own CSV bytes.

## Positions capture

The driver opens the positions tab when present, reads the rendered DOM
table whose header looks position-like (symbol + quantity/value columns), and
persists it as `positions.csv` with the same manifest treatment. When no
positions surface is found, the run still succeeds with a manifest note.

## Fail-closed behaviors

- **No authenticated marker** — the run fails closed with "owner login
  required" and nothing is written (exit code 2).
- **Both trades routes fail** — the run is NON_TERMINAL: `manifest.json`
  records the failed attempts and only `session-evidence.json` (sanitized
  page URL, hashed page title, timestamp) is written (exit code 3).
- **Run directory exists** — create-only enforcement fails the run (exit
  code 4).
- **Credential scan** — before the manifest is finalized, every would-be
  manifest field is scanned for bearer/cookie/token-shaped strings; any hit
  fails the run and the manifest is not written.

## Layout

- `session.py` — CDP attach, navigation/network-idle helpers, login
  detection (playwright is imported lazily; offline tests never import it).
- `pagination.py` — DOM walkers (fixed read-only scripts), view switching,
  the download/state trades routes, and the positions table capture.
- `persistence.py` — create-only run dir, CSV serialization, SHA-256/size
  artifact records, download landing watcher.
- `manifest.py` — counts/hashes-only manifest plus the credential scan.
- `cli.py` — argv-tuple entry point and run orchestration.
