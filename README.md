# Gold Sweep Scanner (XAU/USD → Telegram)

Watches **spot gold (XAU/USD)** — the chart you trade in MetaTrader 5 — on
**15m, 1h, 4h and 1d** candles, every 15 minutes, and sends a Telegram alert
**only when a liquidity sweep / Swing Failure Pattern fires**. No message on
quiet runs. Runs free on GitHub Actions (public repo = unlimited minutes).

Same detection as the multi-exchange sweep-scanner: ATR-filtered ZigZag
swings, a sweep of the nearest *untapped* swing on a closed candle, entry at
the close, stop at the sweep wick, target at 2R, 0–100 quality score.

## Price feed

**OANDA v20 practice API** (free demo account, no funding). Spot gold CFD like
Pepperstone's, **bid** candles (MT5 default), and the 4h / daily candles are
aligned to **17:00 New York** — five daily candles a week, exactly as a
GMT+2/GMT+3 MT5 broker draws them. 15m and 1h candles are hour-aligned
everywhere, so they match too.

Fallback / test feed: `FEED=bitget` uses the Bitget XAUUSDT perpetual (no key).
It tracks gold within a few dollars but trades weekends and its 4h / daily
candles are UTC-midnight, so those levels will not match the MT5 chart.

## One-time setup

1. **Telegram bot** — talk to `@BotFather`, `/newbot`, copy the token.
   Start a chat with the bot (or add it to a group) and send one message.
   Get the chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (`"chat":{"id": ...}`).
2. **OANDA token** — create a free *practice (demo)* account at oanda.com,
   then *My Account → Manage API Access → Generate* (a v20 personal access
   token). Nothing else is needed; the scanner only reads candles.
3. **Repo secrets** — *Settings → Secrets and variables → Actions*:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OANDA_TOKEN`.
4. **Test it** — *Actions → Gold Sweep Scan → Run workflow* with
   *dry run* ticked. The job log prints every timeframe's last candle and the
   message that *would* be sent. Untick dry run to send a real one.

The cron (`*/15 * * * 0-5`, Sunday–Friday) then runs by itself. GitHub does
not guarantee cron timing — ticks can be minutes to hours late under load.
Each run therefore examines the last `LOOKBACK` (3) closed candles of every
timeframe, so a late tick still catches the sweep, and `gold_state.json`
(committed back) makes sure each sweep is alerted exactly once.

## Message

```
🥇 Gold sweep — XAUUSD (OANDA XAUUSD bid) — 03 Oct 2026 14:30 UTC

🔴 Bearish sweep:
  • 1h  @ 4,147.10  ⭐82  · SL 4,162.00 · TP 4,117.30  · candle 03 Oct 13:00 UTC
```

`@` is the signal candle's close (entry), `SL` the sweep wick, `TP` 2R,
`⭐` the quality score, `candle` the signal candle's open time (UTC).

## Knobs (env vars; defaults in `gold_scanner.py`)

| Setting | Default | Meaning |
|---|---|---|
| `FEED` | oanda | `oanda` (real gold) or `bitget` (XAUUSDT perpetual, keyless). |
| `OANDA_ENV` | practice | `practice` or `live` host. |
| `OANDA_PRICE` | B | `B` bid (MT5 default), `M` mid, `A` ask. |
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
| `gold_scanner.py` | Everything: feeds, detection, message, state. |
| `.github/workflows/gold.yml` | 15-min cron + manual run (dry run / feed choice), commits state back. |
| `gold_state.json` | Alerted sweep ids (30-day memory) + last error-notice time. Created on first alert. |
| `gold_signals.jsonl` | One JSON line per alerted sweep (levels + score). |
| `gold_alerts_archive.txt` | Plain-text copy of every message sent. |

## Notes

- A feed problem (bad token, OANDA down) is reported to Telegram at most
  once every 6 hours; the job itself does not fail, so you get no email spam.
- Gold is closed Friday 17:00 → Sunday 17:00 New York; weekend runs simply
  find nothing new. Saturday ticks are skipped by the cron.
- Secrets live only in GitHub Actions secrets — never in code or commits.
