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
| Feed | OANDA was the first choice but the owner **cannot open an OANDA account** (region). Now `FEED=auto`: **TradingView `PEPPERSTONE:XAUUSD`** (the Pepperstone chart itself, no login) → **Dukascopy** public spot-gold bid feed → **Bitget XAUUSDT perp**. OANDA kept as an opt-in (`FEED=oanda`, needs `OANDA_TOKEN`). |
| Candle alignment | Every feed supplies only 15m + 1h bars; 4h / 1d (and 30m/2h/6h/12h) are **built locally** with the trading day at **17:00 New York** (Pepperstone server = GMT+2/+3 following US DST → server midnight = 17:00 NY) and out-of-session bars dropped. TradingView's native 4h bars start at 18:00 NY (session open), one hour off MT5's H4 — do not use them. |
| Timeframes | 15m, 1h, 4h, 1d (`TIMEFRAMES` env can widen). |
| Hosting | Public repo `FaizanTahir450/gold-sweep-scanner`, cron `*/15 * * * 0-5`. Chosen over the private sweep-scanner repo because a 15-min cron ≈ 2,900 billed min/month there. |
| Alerts | **Only when a sweep fires**; silent otherwise. Each sweep alerted once (state file, feed-independent ids). |
| Detection | Identical to sweep-scanner: ATR ZigZag (`ATR_MULT` 2.0), untapped level, sweep on a closed candle, entry = close, stop = wick, target 2R, 0–100 score, `MIN_SCORE` 0, `CANDLES` 120 history per timeframe. |
| Message | One line per sweep: `tf @ entry ⭐score · SL · TP · candle time (UTC)`, grouped 🔴/🟢, header names the feed used. SL/TP shown (single instrument, traded on MT5). |

## Files

| File | Purpose |
|---|---|
| `gold_scanner.py` | Feeds (`tradingview_bars`, `dukascopy_bars`, `bitget_bars`, `oanda_bars` — each returns 15m or 1h base bars), NY calendar (`ny_offset`, `in_session`), `aggregate` / `prepare` (MT5-style candles), detection core (verbatim copy of sweep-scanner's `compute_atr` / `detect_pivots` / `last_intact_swing` / `analyze_sweep` / `score_signal`), `scan_timeframe` (lookback loop), state, log, archive, Telegram, `main`. |
| `.github/workflows/gold.yml` | Cron every 15 min Sun–Fri + `workflow_dispatch` (dry_run, feed). `actions/checkout` uses `ref: main` so a queued run starts from the branch tip (fixes the stale-checkout commit-back conflict seen in sweep-scanner on 2026-09-28). Commits `gold_state.json`, `gold_signals.jsonl`, `gold_alerts_archive.txt` back when they change. |
| `requirements.txt` | `requests`, `websocket-client` (TradingView feed). |
| `gold_state.json` | `{"alerted": {signal_id: unix_ts}, "last_error_notice": unix_ts}`; ids older than 30 days pruned on save. Created by the first alert. |
| `gold_signals.jsonl` | Append-only log of alerted sweeps (id, feed, timeframe, direction, candle_ts, level, entry, stop, target, score, reason). No evaluator yet. |
| `gold_alerts_archive.txt` | Plain-text copy of every message. |

## How a run works

1. `bars_needed()` → how many 15m / 1h base bars the timeframes need (1d needs
   ~3,000 hourly bars for 120 daily candles). `load_base(feed)` fetches them
   from the first feed in `FEED_ORDER` that answers; failures are collected.
2. `prepare(base, tf)`: drop forming and out-of-session bars (`in_session`),
   then `aggregate` into `secs`-long buckets counted from 17:00 New York
   (`_shifted` / `_unshift`, DST via `ny_offset`, US rule since 2007). The
   oldest (possibly partial) bucket and any bucket not yet ended are dropped.
   Verified 2026-10-03: 130/130 locally built daily candles equal TradingView's
   native D bars for PEPPERSTONE:XAUUSD (its D bars are stamped 18:00 NY, ours
   17:00 NY — same content).
3. Each timeframe is trimmed to `CANDLES + LOOKBACK` candles, then
   `scan_timeframe` runs `analyze_sweep` on the arrays truncated at each of the
   last `LOOKBACK` (3) candles — what a run right after each close would have
   seen (no lookahead; same trick as sweep-scanner's backtest). This is the
   defence against GitHub cron drift (the owner's daily crons have been seen
   5 h late): a late or skipped tick still catches a sweep up to 3 candles back.
4. Signal id = `tf:XAUUSD:candle_open_ms` — **no feed name**, so a failover
   to another feed never re-alerts the same candle. Ids in the state file are
   skipped (weekends re-see Friday's candles harmlessly).
5. New sweeps → one Telegram message, archive, JSONL log, state update. All
   feeds failing → one Telegram notice per 6 h; the job never fails on a feed
   error (avoids 96 failure e-mails a day).

## Data sources

- **TradingView** (default): chart websocket
  `wss://data.tradingview.com/socket.io/websocket?from=chart%2F&type=chart`,
  `Origin: https://www.tradingview.com`, frames `~m~<len>~m~<json>`, heartbeat
  `~h~N` must be echoed. Sequence: `set_auth_token ["unauthorized_user_token"]`,
  `chart_create_session`, `switch_timezone Etc/UTC`, `resolve_symbol`,
  `create_series [cs, "s1", "s1", "sym_1", "15"|"60", n]`; bars arrive in
  `timescale_update` / `du` as `[t, o, h, l, c, vol]`, finish on
  `series_completed`. 3,000 hourly bars work without login. Symbol meta for
  PEPPERSTONE:XAUUSD: timezone America/New_York, session `1800-1659:2345|1800-1655:6`.
  Unofficial protocol — may break; hence the failover chain. Symbol search
  (`symbol-search.tradingview.com/symbol_search/v3/`) needs browser-like
  `Origin`/`Referer` headers or returns 403.
- **Dukascopy**: `https://freeserv.dukascopy.com/2.0/?path=chart/json3&instrument=XAU/USD&offer_side=B&interval=15MIN|1HOUR&limit=…&time_direction=P&timestamp=<ms>&jsonp=…`
  with browser UA + `Referer`/`Origin: https://freeserv.dukascopy.com`. JSONP,
  newest-first `[ts_ms, o, h, l, c, vol]`. **`time_direction=P` is required**
  (`N` returns `[]`; `instrument=XAUUSD` returns `[null]`). Bid-side quotes;
  Friday-close bars can differ from Pepperstone by a few dollars (wide spreads).
- **Bitget USDT-M**: `/api/v2/mix/market/candles` (≤1000) + `/history-candles`
  (200/page, `endTime`) for `XAUUSDT`, granularity `15m`/`1H`. Keyless, trades
  weekends (filtered out by `in_session`), only ~90 days of history (listed
  mid-2026) → ~65 daily candles. Verified live 2026-10-03.
- **OANDA v20** (opt-in): `GET {host}/v3/instruments/XAU_USD/candles` M15/H1,
  `price=B`, `Accept-Datetime-Format: UNIX`, Bearer token. Never exercised live
  (owner has no account).
- Pepperstone gold hours (their site, 2026-10-02): 01:01–23:59 server time
  Mon–Thu, 01:01–23:55 Fri, closed weekends; server GMT+3 in US DST, GMT+2
  otherwise.
- Rejected: Twelve Data (XAU/USD is a "commodity" → paid Grow plan only),
  Finnhub (forex candles not on the free plan), Yahoo `XAUUSD=X` (404; `GC=F`
  is COMEX futures), MT5 itself (Windows-only terminal).

## Running locally

```bash
pip install -r requirements.txt
DRY_RUN=1 python gold_scanner.py                 # auto feed, prints what it would send
FEED=dukascopy DRY_RUN=1 python gold_scanner.py  # force a feed
```
Point `STATE_FILE` / `SIGNALS_LOG` / `ALERTS_ARCHIVE` at scratch paths for
local non-dry runs so the repo files are not touched.

## Conventions (shared with the owner's other scanner repos)

Plain-text Telegram (no parse_mode, chunks < 4000 chars), secrets only via env
vars / Actions secrets, `DRY_RUN=1`, state files committed back by the Action,
commits authored as `Muhammed Faizan <FaizanTahir450@users.noreply.github.com>`
with no Co-Authored-By trailer.
