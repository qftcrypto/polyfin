"""Weekly finance markets: "will X hit $H this week" touch markets.

Kept apart from the daily trader: own tables (schema `weekly`), own recorder
process.  Runs on the laptop, not the production server (shared, limited disk):
    .venv/bin/python -m polyfin.weekly.backfill      # resolved weeks + 1h bars
    .venv/bin/python -m polyfin.weekly.recorder      # live weeks, 1m history, books
    .venv/bin/python -m polyfin.weekly.backtest
    .venv/bin/python -m polyfin.weekly.trader        # paper: buy No on overpriced touches
    .venv/bin/python -m polyfin.weekly.report  Token-level price history and books share public.pm_history /
public.pm_books (token ids are unique).
"""
