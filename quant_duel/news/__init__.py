"""News for node A only: poll RSS feeds, map headlines to tickers, score
sentiment with the local LLM, and aggregate a per-ticker daily value from
headlines published BEFORE that day's feature cutoff. Node B never reads
any of this (the CLI refuses, and the daily step never passes sentiment)."""
