# Portfolio123 API routing and costs

## Canonical current sources

- Technical API and Swagger specification: <https://api.portfolio123.com/>
- Official API collection: <https://portfolio123.customerly.help/en/collections/5267-dataminer-api>
- Official credit and monthly-cap policy: <https://portfolio123.customerly.help/en/articles/13766-api-credits>
- Official strategy operations and costs: <https://portfolio123.customerly.help/en/articles/43325-api-strategy>
- Official data-retrieval operations and costs: <https://portfolio123.customerly.help/en/articles/43321-api-data-retrieval>

Acquire the current operation-specific cost from the signed-in provider table and
the corresponding official help article immediately before authorization. Do not
hard-code cost numbers: Portfolio123 has changed operation pricing, and dynamic
operations depend on returned data points. The response-reported `cost` and
`quotaRemaining` values are final run evidence.

## Route contract

HAR remains first for strategy summaries, holdings, performance, transactions,
and trading-system evidence when the reviewed browser route supports them.

The API is allowed only when:

- the repository capability registry declares the operation unsupported by HAR;
  or
- the user explicitly requests API transport.

Authentication, network, browser, schema, or extraction failure is not an API
fallback reason.

Every API decision requires `ApiAuthorization` with:

- exact wrapper operation;
- current credit-table acquisition time;
- estimated credits and authorized budget;
- issue or user approval reference;
- canonical technical and cost documentation anchors.

Call `record_api_result` with response-reported cost and remaining quota. Stop if
actual cost exceeds the approved budget.

## Reviewed operations

| Capability | Wrapper operation | Effect | HAR status |
|---|---|---|---|
| Strategy summary/performance | `strategy` | Metered read | HAR first |
| Historical holdings | `strategy_holdings` | Metered read | HAR first |
| Strategy transactions | `strategy_transactions` | Metered read | HAR first |
| Trading-system definition | `strategy_trading_system` | Metered read | HAR first |
| Rebalance recommendations | `strategy_rebalance` | Metered read | Unsupported by HAR |
| Screen run/backtest | `screen_run`, `screen_backtest` | Metered read | Unsupported by HAR |
| Rank results/performance | `rank_ranks`, `rank_perf` | Metered read | Unsupported by HAR |
| Prices/universe dataset | `data_prices`, `data_universe` | Metered read | Unsupported by HAR |
| Commit rebalance | `strategy_rebalance_commit` | Provider mutation | Unsupported by HAR; separate approval |
| Import/delete transactions | `strategy_transaction_import`, `strategy_transaction_delete` | Provider mutation | Unsupported by HAR; separate approval |

Do not expose a generic request method. Do not invoke create, update, upload,
rerun, copy, commit, import, or delete operations without an exact mutation
capability and separate approval.
