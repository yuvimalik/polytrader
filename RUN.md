# How to run the bot

## Quick run (paper trading)

**PowerShell (Windows):**
```powershell
cd C:\Users\yuv05\OneDrive\Documents\GitHub\polytrader
# Load .env from current dir if present
.\run_bot.ps1
# Or run once without the restart loop:
python polymarket_btc_bot.py --dry-run
```

**Command line (any shell):**
```powershell
python polymarket_btc_bot.py --dry-run
```

Paper trading uses `--dry-run` (default in `run_bot.ps1`). No live orders; trades are logged to `trades.jsonl`.

## Live trading

Set in `.env`: `PK`, `FUNDER`, and optionally Slack webhooks. Then:

```powershell
python polymarket_btc_bot.py
# Or with PowerShell script (live):
.\run_bot.ps1
# (edit run_bot.ps1 to pass no args for live, or add -Live and handle it)
```

To run live with the script, either change the default in `run_bot.ps1` or run:
```powershell
python polymarket_btc_bot.py
```

## Optional arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Paper trading only |
| `--entry-threshold` | 0.91 | Min bid to enter |
| `--stop-loss` | 0.60 | Stop-loss trigger |
| `--size` | 500 | Entry size (USD) |
| `--log-level` | INFO | DEBUG, INFO, WARNING, ERROR |

Example:
```powershell
python polymarket_btc_bot.py --dry-run --entry-threshold 0.90 --size 300
```

## P/L summaries

- **Every trade:** After each exit (stop-loss or resolution), the log shows `P/L this trade: $X.XX`.
- **Every hour:** Every 3600s the bot logs `P/L summary [1h]: N trades | $X.XX` and `P/L summary [24h]: M trades | $Y.YY` (exit trades only, from `trades.jsonl`).

## Trade and resolution tracking

Every round-trip (entry + its exit) has a unique **trade_id** (12-char hex). It is written on ENTRY and on the matching EXIT_STOP_LOSS or EXIT_RESOLUTION so you can track each trade end-to-end.

- **trades.jsonl** is one JSON object per line. Each record includes `trade_id`, `action`, `market_slug`, `side`, `pnl`, and for **EXIT_RESOLUTION** only: `resolution_outcome` ("Up" or "Down"), `resolution_won` (true/false).
- To see a full trade: `rg "trade_id\":\"<id>" trades.jsonl` (or search in a text editor).
- To list resolutions: `rg "EXIT_RESOLUTION" trades.jsonl` and inspect `resolution_outcome` and `resolution_won` for each line.
- When a position resolves (price goes to 0 or 1), the exit row has `action=EXIT_RESOLUTION`, `resolution_outcome` = winning market outcome, and `resolution_won` = whether our side won, so you can reconcile every resolution with the same `trade_id` as the ENTRY.

## Loss timing diagnostics

When a trade is logged as a loss (`STOP_LOSS_TRIGGER` or `RESOLUTION_LOSS`), the bot now prints a timestamped **LOSS POST-MORTEM** block in `polytrader.log` and stdout that includes:

- Entry timestamp (`entry_ts`)
- First/last snapshot timestamps while monitoring the position
- Total hold duration and monitor-window duration
- T-minus progression (`entry T-...s -> loss T-...s`) to show how close to resolution the loss occurred
- Snapshot-by-snapshot timeline with UTC timestamp, bid, and depth

## Stop-loss timeframe analysis (most common SL windows)

To see where stop-losses happen most often, run:

```powershell
python3 analyze_stop_loss_timeframes.py --file trades.jsonl --bucket-secs 60 --top 10
```

Useful variants:

```powershell
# Last 24 hours only
python3 analyze_stop_loss_timeframes.py --file trades.jsonl --last-hours 24

# Finer-grained windows (30-second buckets)
python3 analyze_stop_loss_timeframes.py --file trades.jsonl --bucket-secs 30 --top 20
```
