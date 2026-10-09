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
python -m quant_duel.cli backtest          # baselines + logistic vs buy-and-hold
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
  models/             fit / predict_proba: always-up, persistence, logistic
  backtest/           walk-forward folds (1-day embargo), long/flat with costs, metrics
  cli.py
tests/                timing, folds, costs, ledger maths, leakage canaries
```

## Phases

1. **Done** — skeleton, config, ingest, features, baselines, walk-forward
   backtester with costs, leakage tests.
2. Gradient boosting (LightGBM, else scikit-learn) and model comparison.
3. Paper ledger with the frozen control and SPY, `daily`, exports.
4. LLM tuner (schema, bounds, validation gate, change log) and daily report.
5. News and the sentiment overlay for node A (collect hourly, score in one
   batch before the cutoff).
6. `compare`, `replay`, full dry run.
7. Scheduling with `run-due` (Task Scheduler or cron), deployment notes.

## Leakage rules

Features for day t use data up to t's close only (a test rebuilds the
features from prices cut at day T and requires every earlier row to be
identical). Splits are time-ordered with a one-day embargo; scalers live
inside the fitted model. A feature equal to the future target is refused;
a shuffled target scores ~50%. Results above ~55% accuracy, or a Sharpe far
above buy-and-hold, are flagged as suspicious, not reported as wins.
