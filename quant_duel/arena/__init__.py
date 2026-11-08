"""The Model Arena: local LLMs trade paper books against each other.

Every contestant is one model (arena.yaml) with its own pretend $10,000.
Each trading morning (the 2am run) a model gets the same packet: the
previous close and key stats for every stock, recent headlines, and its own
cash and holdings. It may ask for a few extra pieces of data, then names the
holdings it wants as percents of its money. The order fills at that day's
open, with a cost per dollar traded. The most money at the end wins.

Paper trading only: nothing in this package can place a real order.

    python -m quant_duel.arena run      # the daily job (2am)
    python -m quant_duel.arena report   # the leaderboard (Sundays)
    python -m quant_duel.arena status
"""
