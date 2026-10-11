# Pi Quant Duel

Does news help an LLM-tuned trading model? Two configurations of one
paper-trading system — **A** reads news, **B** never does — run side by side
for a month, then a paired comparison says whether A predicted better.

**Paper trading only. Nothing here can place an order.**

It runs on whatever machines you already have (your Council PCs or Pis):
both nodes are configurations (`nodes/A.yaml`, `nodes/B.yaml`), not
dedicated computers. Prices are fetched once into `data/shared/` and both
nodes read the same bytes; each node keeps its own ledger and change log in
`data/A/` and `data/B/`. The LLM is any OpenAI-compatible server (Ollama's
`/v1`, or `llama-server`).

## Quick start

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
python -m quant_duel.cli ingest            # yfinance; --source synthetic offline
python -m quant_duel.cli build-features
python -m quant_duel.cli backtest          # baselines, logistic, boosting vs buy-and-hold
python -m quant_duel.cli --node B daily    # after each close: ingest, features, paper step, export
python -m pytest -q
```

## Layout

```
config.yaml           shared settings (identical for both nodes)
nodes/A.yaml, B.yaml  node_id + news_enabled — nothing else may differ
quant_duel/
  config.py           loading; refuses a node file that changes shared settings
  market_calendar.py  NYSE trading days from the holiday rules
  ingest/             DataSource (yfinance, synthetic), parquet cache, day hashes, validation
  features/           features for day t from data <= t; labels = day t+1; leakage guard
  models/             fit / predict_proba: always-up, persistence, logistic,
                      boosting (LightGBM if installed, else scikit-learn)
  backtest/           walk-forward folds (1-day embargo), long/flat with costs, metrics
  paper/              books (live, frozen control, SPY), model specs, SQLite ledger, daily step
  export/             tidy CSVs per node for Power BI / Tableau / compare
  cli.py
tests/                timing, folds, costs, ledger maths, leakage canaries
```

## Phases

1. **Done** — skeleton, config, ingest, features, baselines, walk-forward
   backtester with costs, leakage tests.
2. **Done** — gradient boosting (LightGBM, else scikit-learn's
   HistGradientBoostingClassifier; `model.params.boosting.backend`) and the
   comparison: every model on the same folds and rows, with a paired
   log-loss edge over the best baseline (`ll_edge`, t-stat across days).
3. **Done** — paper ledger with the frozen control and SPY, `daily`,
   `export`.
4. LLM tuner (schema, bounds, validation gate, change log) and daily report.
5. News and the sentiment overlay for node A (collect hourly, score in one
   batch before the cutoff).
6. `compare`, `replay`, full dry run.
7. Scheduling with `run-due` (Task Scheduler or cron), deployment notes.

## Paper trading (`daily`)

Run after each close (`--node A` or `--node B`). For every trading day not
yet processed it: fills the previous close's orders at this day's open
(`paper.fill: open|close`) with `cost_bps` on each traded dollar, marks to
the close, scores yesterday's predictions, then predicts every ticker and
places tomorrow's orders (equal weight, long where P(up) clears the
threshold). `--start` sets the first day of a new ledger; missed days are
caught up in order, each seeing only the data it would have seen live.

Three books per node, in `data/<node>/paper.sqlite`:

- **live** — what the tuner may change (phase 4); starts equal to control;
- **control** — the starting settings, frozen when the ledger is created;
- **spy** — buys the benchmark once and holds it.

Holdings are tracked as market value moved by adjusted returns, so splits
and dividend re-adjustments cannot corrupt the books; cash + holdings =
equity is tested. Models are not pickled: each is a JSON spec (settings,
training window, data hash, prediction fingerprint) that refits
deterministically and is verified on every reuse; both nodes get
byte-identical specs from the same prices. Models refit every
`backtest.test_days` trading days, the same cadence as the backtest.

`export` (also run by `daily`) writes `exports/<node>/`: predictions,
fills, equity, equity_curves, holdings, models, price_hashes, events,
metrics.

## Reading a backtest

`ll_edge` is how much lower a learned model's log loss is than the best
dumb baseline's, averaged per day; `ll_edge_t` is its t-statistic across
days (days, not rows — tickers on one day move together). A negative edge
means the model is worse than not modelling at all; |t| under ~2 is noise.
LightGBM and scikit-learn give close but not identical numbers, so compare
runs only when the printed backend matches.

## Leakage rules

Features for day t use data up to t's close only (a test rebuilds the
features from prices cut at day T and requires every earlier row to be
identical). Splits are time-ordered with a one-day embargo; scalers live
inside the fitted model. A feature equal to the future target is refused;
a shuffled target scores ~50%. Results above ~55% accuracy, or a Sharpe far
above buy-and-hold, are flagged as suspicious, not reported as wins.
