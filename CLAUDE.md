# CLAUDE.md — gold-sweep-scanner

Guidance for future Claude Code sessions working in this repo.

## What this project is

A single-file Python scanner (`gold_scanner.py`) that runs **on GitHub
Actions every 15 minutes**, fetches **spot gold (XAU/USD)** candles on
**15m / 1h / 4h / 1d**, detects **liquidity sweeps / Swing Failure Patterns**
with the same engine as the owner's `sweep-scanner` repo, and sends a Telegram
alert through its **own bot** only when a sweep fires. Public repo (unlimited
Actions minutes). Built 2026-10-03 from the owner's brief: "another workflow
for gold only, 15 minute to 1 day, separate Telegram bot".

## Owner decisions — DO NOT change unless the user explicitly asks

| Decision (2026-10-02/03) | Choice |
|---|---|
| Instrument | The **real gold chart as on MetaTrader 5 / Pepperstone** — not PAXG/XAUT tokens, not a crypto perp. |
| Feed | **OANDA v20 practice API** (free demo account + token). Bid candles; `dailyAlignment=17`, `alignmentTimezone=America/New_York` so 4h/1d candles match an MT5 GMT+2/+3 broker (Pepperstone server time follows US DST → daily close at 17:00 New York). |
| Timeframes | 15m, 1h, 4h, 1d (`TIMEFRAMES` env can widen to 30m/2h/6h/12h). |
| Hosting | New **public** repo `FaizanTahir450/gold-sweep-scanner`, cron `*/15 * * * 0-5`. Chosen over the private sweep-scanner repo because a 15-min cron ≈ 2,900 billed min/month there. |
| Alerts | **Only when a sweep fires**; silent otherwise. Each sweep alerted once (state file). |
| Detection | Identical to sweep-scanner: ATR ZigZag (`ATR_MULT` 2.0), untapped level (`REQUIRE_UNTAPPED`), sweep on a closed candle, entry = close, stop = wick, target 2R, 0–100 score, `MIN_SCORE` 0. |
| Message | One line per sweep: `tf @ entry ⭐score · SL · TP · candle time (UTC)`, grouped 🔴/🟢. SL/TP are shown here (single instrument, traded on MT5) unlike the crypto scanner. |

## Files

| File | Purpose |
|---|---|
| `gold_scanner.py` | Feeds (`oanda_candles`, `bitget_candles`), detection core (verbatim copy of sweep-scanner's `compute_atr` / `detect_pivots` / `last_intact_swing` / `analyze_sweep` / `score_signal`), `scan_timeframe` (lookback loop), state, log, archive, Telegram, `main`. |
| `.github/workflows/gold.yml` | Cron every 15 min Sun–Fri + `workflow_dispatch` (dry_run, feed). `actions/checkout` uses `ref: main` so a queued run starts from the branch tip (fixes the stale-checkout commit-back conflict seen in sweep-scanner on 2026-09-28). Commits `gold_state.json`, `gold_signals.jsonl`, `gold_alerts_archive.txt` back when they change. |
| `gold_state.json` | `{"alerted": {signal_id: unix_ts}, "last_error_notice": unix_ts}`; ids older than 30 days are pruned on save. Created by the first alert. |
| `gold_signals.jsonl` | Append-only log of alerted sweeps (id, feed, timeframe, direction, candle_ts, level, entry, stop, target, score, reason). No evaluator yet. |
| `gold_alerts_archive.txt` | Plain-text copy of every message. |

## How a run works

1. For each timeframe fetch `CANDLES` (120) closed candles (`complete == true`
   on OANDA; `start + length <= now` on Bitget).
2. `scan_timeframe` runs `analyze_sweep` on the arrays truncated at each of
   the last `LOOKBACK` (3) candles — exactly what a run right after each close
   would have seen (no lookahead; same trick as sweep-scanner's backtest). This
   is the defence against GitHub cron drift (the owner's daily crons have been
   observed 5 h late): a late or skipped tick still catches a sweep up to
   3 candles back (45 min on 15m, 3 h on 1h, 12 h on 4h, 3 days on 1d).
3. Signal id = `tf:feed:XAUUSD:candle_open_ms`. Ids already in the state file
   are skipped, so nothing is alerted twice (weekends re-see Friday's candles).
4. New sweeps → one Telegram message, archive, JSONL log, state update.
   Feed errors → one Telegram notice per 6 h; the job never fails on a feed
   error (avoids 96 failure e-mails a day).

## Data sources

- **OANDA v20**: `GET {host}/v3/instruments/XAU_USD/candles` with
  `granularity`, `count` (max 5000), `price=B`, `dailyAlignment=17`,
  `alignmentTimezone=America/New_York`, header `Authorization: Bearer <token>`
  and `Accept-Datetime-Format: UNIX` (times as `"1759428900.000000000"`).
  Host `api-fxpractice.oanda.com` (practice) / `api-fxtrade.oanda.com` (live).
  Per the docs M15/H1 are hour-aligned, H4/D are "day alignment" (honour the
  two alignment params). Response candles carry `complete`, `volume` (tick
  volume), and `bid`/`mid`/`ask` OHLC as strings. Gold trades Sun 17:00 →
  Fri 17:00 New York with a daily break around 17:00.
  **Not yet exercised against the live API** (no token at build time) — the
  request/parsing path was verified with a faked response; the first dry-run
  dispatch with the owner's token is the live check.
- **Bitget USDT-M**: `GET api.bitget.com/api/v2/mix/market/candles?symbol=XAUUSDT&productType=USDT-FUTURES&granularity=15m|1H|4H|1Dutc&limit=…`
  → ascending `[start_ms, o, h, l, c, baseVol, quoteVol]`, limit ≤ 1000, no
  `2H`. Verified live on 2026-10-03. Keyless; UTC-aligned; trades weekends.
- Rejected: Yahoo `XAUUSD=X` (404 from the chart API as of 2026-10-02; `GC=F`
  futures works but is COMEX, not spot), Twelve Data (needs a key; 4h/1d would
  have to be rebuilt locally with the NY alignment), MT5 itself (Windows-only
  terminal, cannot run on a Linux runner).

## Running locally

```bash
pip install requests
# keyless smoke test on the Bitget perp, nothing sent/written:
FEED=bitget DRY_RUN=1 python gold_scanner.py
# real feed:
OANDA_TOKEN=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python gold_scanner.py
```
Point `STATE_FILE` / `SIGNALS_LOG` / `ALERTS_ARCHIVE` at scratch paths for
local non-dry runs so the repo files are not touched.

## Conventions (shared with the owner's other scanner repos)

Plain-text Telegram (no parse_mode, chunks < 4000 chars), secrets only via env
vars / Actions secrets, `DRY_RUN=1`, state files committed back by the Action,
commits authored as `Muhammed Faizan <FaizanTahir450@users.noreply.github.com>`
with no Co-Authored-By trailer.
