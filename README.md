# Gold Sweep Scanner (XAU/USD → Telegram)

Watches **spot gold (XAU/USD)** — the chart you trade in MetaTrader 5 — on
**15m, 1h, 4h and 1d** candles, every 15 minutes, and sends a Telegram alert
**only when a liquidity sweep / Swing Failure Pattern fires**. No message on
quiet runs. Runs free on GitHub Actions (public repo = unlimited minutes).
No broker account, no API key.

Same detection as the multi-exchange sweep-scanner: ATR-filtered ZigZag
swings, a sweep of the nearest *untapped* swing on a closed candle, entry at
the close, stop at the sweep wick, target at 2R, 0–100 quality score.

## Price feed — the Pepperstone chart itself

By default the scanner reads **`PEPPERSTONE:XAUUSD` from TradingView**, the
same feed your MT5 terminal shows. If TradingView does not answer, it falls
back to **Dukascopy's** public spot-gold feed (bid candles), and after that to
the **Bitget XAUUSDT perpetual**. Every run says which feed it used.

Candles are built the way MT5 draws them: the feed delivers 15m and 1h bars
(hour-aligned, identical on every platform), and the scanner builds 4h and
daily candles itself with the trading day starting at **17:00 New York** —
Pepperstone's server runs GMT+2/GMT+3 following US daylight saving, so server
midnight *is* 17:00 New York. Weekend and daily-break (17:00–17:59 NY) bars are
dropped. This matters: TradingView's own 4h bars start at 18:00 NY, one hour
off from MT5's H4, so they are deliberately not used. The locally built daily
candles were checked against TradingView's daily bars: 130 of 130 identical.

## One-time setup

1. **Telegram bot** — talk to `@BotFather`, `/newbot`, copy the token.
   Start a chat with the bot (or add it to a group) and send one message.
   Get the chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (`"chat":{"id": ...}`).
2. **Repo secrets** — *Settings → Secrets and variables → Actions*:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. (Optional `OANDA_TOKEN` only if
   you ever want `FEED=oanda`.)
3. **Test it** — *Actions → Gold Sweep Scan → Run workflow* with
   *dry run* ticked. The job log prints every timeframe's last candle and the
   message that *would* be sent. Untick dry run to send a real one.

The cron (`*/15 * * * 0-5`, Sunday–Friday) then runs by itself. GitHub does
not guarantee cron timing — ticks can be minutes to hours late under load.
Each run therefore examines the last `LOOKBACK` (3) closed candles of every
timeframe, so a late tick still catches the sweep, and `gold_state.json`
(committed back) makes sure each sweep is alerted exactly once — even if the
feed changes between runs.

## Message

```
🥇 Gold sweep — XAUUSD (TradingView PEPPERSTONE:XAUUSD) — 03 Oct 2026 14:30 UTC

🔴 Bearish sweep:
  • 1h  @ 4,147.10  ⭐82  · SL 4,162.00 · TP 4,117.30  · candle 03 Oct 13:00 UTC
```

`@` is the signal candle's close (entry), `SL` the sweep wick, `TP` 2R,
`⭐` the quality score, `candle` the signal candle's open time (UTC).

## Knobs (env vars; defaults in `gold_scanner.py`)

| Setting | Default | Meaning |
|---|---|---|
| `FEED` | auto | `auto` = tradingview → dukascopy → bitget; or force one of `tradingview`, `dukascopy`, `bitget`, `oanda`. |
| `TV_SYMBOL` | PEPPERSTONE:XAUUSD | TradingView symbol to read (any broker feed, e.g. `OANDA:XAUUSD`). |
| `TIMEFRAMES` | 15m,1h,4h,1d | Any of 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d. |
| `LOOKBACK` | 3 | Closed candles examined per timeframe each run (drift safety). |
| `ATR_MULT` | 2.0 | ATR multiple a close must reverse by to confirm a swing. |
| `REQUIRE_UNTAPPED` | 1 | Only first-time sweeps; `0` to allow re-sweeps. |
| `TARGET_R` | 2 | Reward multiple for the target (stop = 1R). |
| `MIN_SCORE` | 0 | Opt-in quality filter (0 = alert every sweep). |
| `DRY_RUN` | 0 | `1` = print the message, send nothing, write nothing. |

## Files

| File | Purpose |
|---|---|
| `gold_scanner.py` | Everything: feeds, MT5-style candle building, detection, message, state. |
| `.github/workflows/gold.yml` | 15-min cron + manual run (dry run / feed choice), commits state back. |
| `gold_state.json` | Alerted sweep ids (30-day memory) + last error-notice time. Created on first alert. |
| `gold_signals.jsonl` | One JSON line per alerted sweep (levels + score + feed used). |
| `gold_alerts_archive.txt` | Plain-text copy of every message sent. |

## Notes

- If every feed fails, one Telegram warning is sent at most every 6 hours;
  the job itself does not fail, so you get no e-mail spam.
- Gold is closed Friday 17:00 → Sunday 18:00 New York; weekend runs simply
  find nothing new. Saturday ticks are skipped by the cron.
- The TradingView and Dukascopy feeds are public but undocumented; that is why
  there are three of them and automatic failover.
- Secrets live only in GitHub Actions secrets — never in code or commits.
