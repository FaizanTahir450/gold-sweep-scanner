"""
Gold Liquidity Sweep (SFP) Scanner — XAU/USD, 15 minutes to 1 day
------------------------------------------------------------------
Scans ONE instrument — spot gold (XAU/USD), the chart you see in MetaTrader 5
— on 15m / 1h / 4h / 1d candles every 15 minutes (GitHub Actions cron) and
sends a Telegram alert ONLY when a liquidity sweep / Swing Failure Pattern
fires. Detection is identical to sweep-scanner:

  * ATR-filtered ZigZag swings (a low/high only counts once price CLOSES away
    from it by ATR_MULT x ATR), see detect_pivots.
  * The nearest confirmed swing whose level is still intact (no close beyond
    it, and with REQUIRE_UNTAPPED no wick through it either).
  * Signal on a CLOSED candle:
      Bullish sweep : low  < swing_low  AND close > swing_low
      Bearish sweep : high > swing_high AND close < swing_high
    Entry = close, stop = the sweep wick, target = TARGET_R x risk, 0-100 score.

Feed: OANDA v20 (free practice account) — spot gold CFD like Pepperstone's,
bid candles, 4h and daily candles aligned to 17:00 New York (five candles a
week, the MT5 convention). FEED=bitget switches to the Bitget XAUUSDT
perpetual (keyless; UTC-aligned, trades weekends) for tests or as a fallback.

Robust to GitHub's cron drift: every run examines the last LOOKBACK closed
candles of each timeframe (not only the newest), and gold_state.json
(committed back) guarantees each sweep is alerted exactly once.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OANDA_TOKEN (required unless
FEED=bitget). Optional: FEED, OANDA_ENV, OANDA_PRICE, OANDA_INSTRUMENT,
TIMEFRAMES, LOOKBACK, ATR_MULT, REQUIRE_UNTAPPED, TARGET_R, MIN_SCORE,
DRY_RUN, STATE_FILE, SIGNALS_LOG, ALERTS_ARCHIVE.
"""

import os
import json
import time
from collections import namedtuple

import requests

# ── Config ─────────────────────────────────────────────────────────
FEED             = os.environ.get("FEED", "oanda").strip().lower()          # oanda (real gold) | bitget (XAUUSDT perp)
OANDA_ENV        = os.environ.get("OANDA_ENV", "practice").strip().lower()  # practice (free demo account) | live
OANDA_TOKEN      = os.environ.get("OANDA_TOKEN", "").strip()
OANDA_INSTRUMENT = os.environ.get("OANDA_INSTRUMENT", "XAU_USD").strip()
OANDA_PRICE      = (os.environ.get("OANDA_PRICE", "B").strip().upper()[:1] or "B")  # B = bid (MT5 default) | M = mid | A = ask
DAILY_ALIGN_HOUR = 17                        # 4h / daily candles open at 17:00 New York — MT5 / Pepperstone convention
ALIGN_TZ         = "America/New_York"
BITGET_SYMBOL    = "XAUUSDT"                 # Bitget USDT-M gold perpetual (test / fallback feed)

TIMEFRAMES = [t.strip() for t in os.environ.get("TIMEFRAMES", "15m,1h,4h,1d").split(",") if t.strip()]
LOOKBACK   = max(1, int(os.environ.get("LOOKBACK", "3")))   # closed candles examined per timeframe, per run
CANDLES    = 120                                              # history fetched per timeframe
ATR_PERIOD = 14                                               # ATR lookback used by the swing filter
ATR_MULT   = float(os.environ.get("ATR_MULT", "2.0"))        # close-reversal (in ATRs) that confirms a swing
REQUIRE_UNTAPPED = os.environ.get("REQUIRE_UNTAPPED", "1") != "0"   # only FIRST-time sweeps
TARGET_R   = float(os.environ.get("TARGET_R", "2"))          # reward multiple (stop = 1R)
MIN_SCORE  = int(os.environ.get("MIN_SCORE", "0"))           # opt-in quality filter (0 = keep all)
VOL_LOOKBACK = 20                                             # candles for the average-volume baseline
DRY_RUN    = os.environ.get("DRY_RUN", "0").strip() == "1"   # print the message, send nothing, write nothing

STATE_FILE     = os.environ.get("STATE_FILE", "gold_state.json")         # alerted signal ids (dedup), committed back
SIGNALS_LOG    = os.environ.get("SIGNALS_LOG", "gold_signals.jsonl")     # one JSON line per alerted sweep
ALERTS_ARCHIVE = os.environ.get("ALERTS_ARCHIVE", "gold_alerts_archive.txt")  # plain-text copy of every message
STATE_KEEP_DAYS     = 30            # forget alerted ids older than this
ERROR_NOTICE_EVERY  = 6 * 3600      # at most one "feed error" Telegram notice per 6 h
MARKET_CLOSED_AFTER = 2 * 3600      # newest 15m candle older than this → market closed (log only)

TF = {   # per-timeframe feed granularity + candle length (seconds)
    "15m": {"oanda": "M15", "bitget": "15m",    "secs": 900},
    "30m": {"oanda": "M30", "bitget": "30m",    "secs": 1800},
    "1h":  {"oanda": "H1",  "bitget": "1H",     "secs": 3600},
    "2h":  {"oanda": "H2",  "bitget": None,     "secs": 7200},
    "4h":  {"oanda": "H4",  "bitget": "4H",     "secs": 14400},
    "6h":  {"oanda": "H6",  "bitget": "6Hutc",  "secs": 21600},
    "12h": {"oanda": "H12", "bitget": "12Hutc", "secs": 43200},
    "1d":  {"oanda": "D",   "bitget": "1Dutc",  "secs": 86400},
}
OANDA_HOST = "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"
BITGET_BASE = "https://api.bitget.com"
FEED_LABEL = {"oanda": f"OANDA {OANDA_INSTRUMENT.replace('_', '')} {dict(B='bid', M='mid', A='ask')[OANDA_PRICE]}",
              "bitget": f"Bitget {BITGET_SYMBOL} perp"}

# Parallel per-candle arrays (ascending, closed candles only). `opens` are candle
# start times in ms (each candle's id); `oprices` open prices; `vols` volume.
Klines = namedtuple("Klines", "highs lows closes opens oprices vols")

session = requests.Session()
session.headers.update({"User-Agent": "gold-sweep-scanner/1.0", "Accept": "application/json"})


# ── Feeds (return Klines; ascending, closed candles only) ──────────
def oanda_candles(tf, count=CANDLES):
    """OANDA v20 candles. `complete` marks closed candles; H4/D are aligned to
    DAILY_ALIGN_HOUR in ALIGN_TZ (17:00 New York = the MT5 daily close)."""
    if not OANDA_TOKEN:
        raise RuntimeError("OANDA_TOKEN is not set")
    r = session.get(f"{OANDA_HOST}/v3/instruments/{OANDA_INSTRUMENT}/candles",
                    params={"granularity": TF[tf]["oanda"], "count": min(count + 1, 5000),
                            "price": OANDA_PRICE, "dailyAlignment": DAILY_ALIGN_HOUR,
                            "alignmentTimezone": ALIGN_TZ, "smooth": "false"},
                    headers={"Authorization": f"Bearer {OANDA_TOKEN}", "Accept-Datetime-Format": "UNIX"},
                    timeout=25)
    r.raise_for_status()
    key = {"B": "bid", "M": "mid", "A": "ask"}[OANDA_PRICE]
    rows = [c for c in (r.json().get("candles") or []) if c.get("complete")][-count:]
    return Klines(
        highs=[float(c[key]["h"]) for c in rows],
        lows=[float(c[key]["l"]) for c in rows],
        closes=[float(c[key]["c"]) for c in rows],
        opens=[int(float(c["time"]) * 1000) for c in rows],   # start time (ms) — candle id
        oprices=[float(c[key]["o"]) for c in rows],
        vols=[float(c.get("volume") or 0) for c in rows],      # tick volume (same as MT5)
    )


def bitget_candles(tf, count=CANDLES):
    """Bitget USDT-M perpetual candles, ascending [start_ms, o, h, l, c, baseVol, quoteVol].
    Only a start time is given, so a candle is closed once start + length <= now."""
    gran = TF[tf]["bitget"]
    if not gran:
        raise ValueError(f"Bitget has no {tf} candles")
    r = session.get(f"{BITGET_BASE}/api/v2/mix/market/candles",
                    params={"symbol": BITGET_SYMBOL, "productType": "USDT-FUTURES",
                            "granularity": gran, "limit": min(count + 1, 1000)}, timeout=25)
    r.raise_for_status()
    now_ms = time.time() * 1000
    rows = [x for x in (r.json().get("data") or []) if int(x[0]) + TF[tf]["secs"] * 1000 <= now_ms][-count:]
    return Klines(
        highs=[float(x[2]) for x in rows], lows=[float(x[3]) for x in rows],
        closes=[float(x[4]) for x in rows], opens=[int(x[0]) for x in rows],
        oprices=[float(x[1]) for x in rows], vols=[float(x[5]) for x in rows],
    )


FEEDS = {"oanda": oanda_candles, "bitget": bitget_candles}


# ── Swing detection (ATR-filtered ZigZag — identical to sweep-scanner) ──
def compute_atr(highs, lows, closes, period=ATR_PERIOD):
    """Average True Range (simple rolling mean of TR); one value per candle."""
    n = len(closes)
    trs = [highs[0] - lows[0]] if n else []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr, s = [], 0.0
    for i, tr in enumerate(trs):
        s += tr
        if i >= period:
            s -= trs[i - period]
            atr.append(s / period)
        else:
            atr.append(s / (i + 1))
    return atr


def detect_pivots(highs, lows, closes, atr_mult=None, atr=None):
    """
    ZigZag swing detector with an ATR noise filter.

    While price keeps making higher highs, the candidate swing high keeps
    extending — so the TRUE extreme is always captured, never an early bump.
    The candidate is only CONFIRMED as a pivot once price CLOSES against it
    by atr_mult * ATR; smaller wiggles never confirm and are ignored.
    Pivots alternate H, L, H, L. Returns a time-ordered list of dicts
    {idx, price, type 'H'/'L', confirmed} — `confirmed` is the bar at which
    the pivot became known (no lookahead).
    """
    n = len(closes)
    if n < 3:
        return []
    if atr is None:
        atr = compute_atr(highs, lows, closes)
    if atr_mult is None:
        atr_mult = ATR_MULT

    piv = []
    trend = 0                     # 0 = warm-up, +1 = up-leg, -1 = down-leg
    cand = hi_i = lo_i = 0        # candidate extreme / warm-up running extremes

    for i in range(1, n):
        if trend == 0:
            if highs[i] > highs[hi_i]:
                hi_i = i
            if lows[i] < lows[lo_i]:
                lo_i = i
            if closes[i] - lows[lo_i] >= atr_mult * atr[i]:
                piv.append({"idx": lo_i, "price": lows[lo_i], "type": "L", "confirmed": i})
                seg = range(lo_i + 1, i + 1)
                cand = max(seg, key=lambda j: highs[j]) if len(seg) else i
                trend = 1
            elif highs[hi_i] - closes[i] >= atr_mult * atr[i]:
                piv.append({"idx": hi_i, "price": highs[hi_i], "type": "H", "confirmed": i})
                seg = range(hi_i + 1, i + 1)
                cand = min(seg, key=lambda j: lows[j]) if len(seg) else i
                trend = -1
        elif trend == 1:                                   # tracking a swing HIGH
            if highs[i] > highs[cand]:
                cand = i
            elif highs[cand] - closes[i] >= atr_mult * atr[i]:
                piv.append({"idx": cand, "price": highs[cand], "type": "H", "confirmed": i})
                cand = min(range(cand + 1, i + 1), key=lambda j: lows[j])
                trend = -1
        else:                                              # tracking a swing LOW
            if lows[i] < lows[cand]:
                cand = i
            elif closes[i] - lows[cand] >= atr_mult * atr[i]:
                piv.append({"idx": cand, "price": lows[cand], "type": "L", "confirmed": i})
                cand = max(range(cand + 1, i + 1), key=lambda j: highs[j])
                trend = 1
    return piv


def last_intact_swing(pivots, highs, lows, closes, kind):
    """
    Most recent confirmed swing low/high whose level is still sweepable:
    confirmed BEFORE the last candle, no candle CLOSED beyond it since the
    pivot, and (with REQUIRE_UNTAPPED) no wick traded through it either.
    A broken nearer level falls through to the next older swing.
    Returns (price, idx) or (None, None).
    """
    last = len(closes) - 1
    want = "L" if kind == "low" else "H"
    for p in reversed(pivots):
        if p["type"] != want or p["confirmed"] >= last:
            continue
        i, v = p["idx"], p["price"]
        if kind == "low":
            if any(c <= v for c in closes[i + 1:last]):
                continue
            if REQUIRE_UNTAPPED and any(lo < v for lo in lows[i + 1:last]):
                continue
        else:
            if any(c >= v for c in closes[i + 1:last]):
                continue
            if REQUIRE_UNTAPPED and any(hi > v for hi in highs[i + 1:last]):
                continue
        return v, i
    return None, None


def analyze_sweep(highs, lows, closes):
    """Sweep of the nearest intact swing on the LAST candle of the arrays.
    Returns {direction, level, entry, stop, target, level_idx, atr} or None."""
    n = len(closes)
    if n < ATR_PERIOD + 10:
        return None
    last = n - 1
    atr = compute_atr(highs, lows, closes)
    pivots = detect_pivots(highs, lows, closes, atr=atr)
    entry = closes[last]

    level, pidx = last_intact_swing(pivots, highs, lows, closes, "low")
    if level is not None and lows[last] < level and closes[last] > level:
        stop = lows[last]
        risk = entry - stop
        if risk > 0:
            return {"direction": "bull", "level": level, "entry": entry,
                    "stop": stop, "target": entry + TARGET_R * risk,
                    "level_idx": pidx, "atr": atr[last]}

    level, pidx = last_intact_swing(pivots, highs, lows, closes, "high")
    if level is not None and highs[last] > level and closes[last] < level:
        stop = highs[last]
        risk = stop - entry
        if risk > 0:
            return {"direction": "bear", "level": level, "entry": entry,
                    "stop": stop, "target": entry - TARGET_R * risk,
                    "level_idx": pidx, "atr": atr[last]}
    return None


def score_signal(sig, k):
    """0-100 ranking score (volume spike, rejection wick, close location,
    reclaim distance, swing size) — never changes detection."""
    highs, lows, closes, oprices, vols = k.highs, k.lows, k.closes, k.oprices, k.vols
    i = len(closes) - 1
    hi, lo, cl, op = highs[i], lows[i], closes[i], oprices[i]
    rng = (hi - lo) or 1e-9
    a = sig.get("atr") or 1e-9
    level, pidx = sig["level"], sig["level_idx"]

    prior = vols[max(0, i - VOL_LOOKBACK):i]
    avg_v = (sum(prior) / len(prior)) if prior else 0.0
    v_ratio = (vols[i] / avg_v) if avg_v > 0 else 1.0

    if sig["direction"] == "bull":
        close_loc = (cl - lo) / rng
        rej = (min(op, cl) - lo) / rng
        reclaim = (cl - level) / a
        swing = (max(highs[pidx:i]) - level) / a
    else:
        close_loc = (hi - cl) / rng
        rej = (hi - max(op, cl)) / rng
        reclaim = (level - cl) / a
        swing = (level - min(lows[pidx:i])) / a

    s_vol = min(v_ratio / 2.0, 1.0)
    s_rej = min(max(rej, 0.0) / 0.5, 1.0)
    s_loc = min(max(close_loc, 0.0), 1.0)
    s_rec = min(max(reclaim, 0.0) / 0.5, 1.0)
    s_swg = min(max(swing, 0.0) / 4.0, 1.0)
    score = round(100 * (0.30 * s_vol + 0.25 * s_rej + 0.15 * s_loc
                         + 0.15 * s_rec + 0.15 * s_swg))
    reason = (f"vol {v_ratio:.1f}x·rej {rej:.2f}·loc {close_loc:.2f}"
              f"·rcl {reclaim:.2f}A·swg {swing:.1f}A")
    return score, reason


# ── Scan ───────────────────────────────────────────────────────────
def scan_timeframe(tf, k):
    """Run the detector on each of the last LOOKBACK closed candles — exactly
    what a run right after each of those closes would have seen (no lookahead)
    — so a late or skipped cron tick still catches the sweep."""
    out = []
    n = len(k.closes)
    for end in range(max(ATR_PERIOD + 10, n - LOOKBACK + 1), n + 1):
        sub = Klines(*(arr[:end] for arr in k))
        sig = analyze_sweep(sub.highs, sub.lows, sub.closes)
        if sig:
            score, reason = score_signal(sig, sub)
            if score >= MIN_SCORE:
                sig.update({"timeframe": tf, "feed": FEED, "candle_ts": sub.opens[-1],
                            "score": score, "reason": reason})
                out.append(sig)
    return out


def signal_id(s):
    return f"{s['timeframe']}:{s['feed']}:XAUUSD:{s['candle_ts']}"


# ── State / log / archive ──────────────────────────────────────────
def load_state(path=STATE_FILE):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("alerted", {})
    st.setdefault("last_error_notice", 0)
    return st


def save_state(st, path=STATE_FILE):
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    st["alerted"] = {k: v for k, v in st["alerted"].items() if v >= cutoff}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
        f.write("\n")


def log_signals(sigs, path=SIGNALS_LOG):
    now = int(time.time())
    with open(path, "a", encoding="utf-8") as f:
        for s in sigs:
            row = {"id": signal_id(s), "ts": now, "feed": s["feed"], "timeframe": s["timeframe"],
                   "direction": s["direction"], "candle_ts": s["candle_ts"],
                   "level": s["level"], "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                   "score": s["score"], "reason": s["reason"]}
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def append_archive(text, path=ALERTS_ARCHIVE):
    with open(path, "a", encoding="utf-8") as f:
        f.write("=" * 60 + "\n" + text.rstrip() + "\n")


# ── Telegram ───────────────────────────────────────────────────────
def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        r = session.post(url, json={"chat_id": chat_id, "text": c.rstrip(), "disable_web_page_preview": True}, timeout=25)
        r.raise_for_status()


# ── Message ────────────────────────────────────────────────────────
def fmt_price(p):
    return f"{p:,.2f}"


def fmt_ts(ms):
    return time.strftime("%d %b %H:%M", time.gmtime(ms / 1000)) + " UTC"


def fmt_line(s):
    return (f"  • {s['timeframe']:<3} @ {fmt_price(s['entry'])}  ⭐{s['score']}"
            f"  · SL {fmt_price(s['stop'])} · TP {fmt_price(s['target'])}"
            f"  · candle {fmt_ts(s['candle_ts'])}")


def render_message(sigs):
    order = {tf: i for i, tf in enumerate(TIMEFRAMES)}
    sigs = sorted(sigs, key=lambda s: (order.get(s["timeframe"], 99), s["candle_ts"]))
    bulls = [s for s in sigs if s["direction"] == "bull"]
    bears = [s for s in sigs if s["direction"] == "bear"]
    parts = [f"🥇 Gold sweep — XAUUSD ({FEED_LABEL[FEED]}) — {time.strftime('%d %b %Y %H:%M', time.gmtime())} UTC"]
    if bears:
        parts.append("🔴 Bearish sweep" + ("s" if len(bears) > 1 else "") + ":\n" + "\n".join(fmt_line(s) for s in bears))
    if bulls:
        parts.append("🟢 Bullish sweep" + ("s" if len(bulls) > 1 else "") + ":\n" + "\n".join(fmt_line(s) for s in bulls))
    return "\n\n".join(parts)


# ── Main ───────────────────────────────────────────────────────────
def main():
    bad = [t for t in TIMEFRAMES if t not in TF]
    if bad or FEED not in FEEDS:
        raise SystemExit(f"Bad config: FEED={FEED} TIMEFRAMES={TIMEFRAMES} (unknown: {bad})")
    fetch = FEEDS[FEED]
    print(f"Gold sweep scan | feed={FEED_LABEL[FEED]} | tfs={','.join(TIMEFRAMES)} | lookback={LOOKBACK}"
          f" | atr_mult={ATR_MULT} untapped={REQUIRE_UNTAPPED} target={TARGET_R}R min_score={MIN_SCORE}"
          f" | dry_run={DRY_RUN}")

    state = load_state()
    new_signals, errors, newest_15m = [], [], None
    for tf in TIMEFRAMES:
        try:
            k = fetch(tf)
            if not k.closes:
                raise RuntimeError("no candles returned")
            sigs = scan_timeframe(tf, k)
            fresh = [s for s in sigs if signal_id(s) not in state["alerted"]]
            new_signals += fresh
            if tf == "15m":
                newest_15m = k.opens[-1] / 1000 + TF[tf]["secs"]
            print(f"[{tf}] {len(k.closes)} candles · last closed {fmt_ts(k.opens[-1])} close {fmt_price(k.closes[-1])}"
                  f" · {len(sigs)} sweep(s) in last {LOOKBACK} · {len(fresh)} new")
            for s in sigs:
                tag = "NEW" if s in fresh else "already alerted"
                print(f"      {s['direction']:4} candle {fmt_ts(s['candle_ts'])} level {fmt_price(s['level'])} "
                      f"entry {fmt_price(s['entry'])} stop {fmt_price(s['stop'])} target {fmt_price(s['target'])} "
                      f"score {s['score']} ({s['reason']}) — {tag}")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = (e.response.text[:120] if e.response is not None else "").replace("\n", " ")
            errors.append(f"{tf}: HTTP {code} {body}")
            print(f"[{tf}] feed error: HTTP {code} {body}")
        except Exception as e:
            errors.append(f"{tf}: {type(e).__name__}: {str(e)[:120]}")
            print(f"[{tf}] feed error: {type(e).__name__}: {e}")
        time.sleep(0.1)

    if newest_15m and time.time() - newest_15m > MARKET_CLOSED_AFTER:
        print(f"Market looks closed (newest 15m candle closed {(time.time() - newest_15m) / 3600:.1f} h ago).")

    changed = False
    if errors:
        text = "⚠️ Gold scanner — feed error (" + FEED_LABEL[FEED] + ")\n" + "\n".join(errors)
        if DRY_RUN:
            print("\n----- DRY RUN: error notice that would be sent -----\n" + text)
        elif time.time() - state["last_error_notice"] >= ERROR_NOTICE_EVERY:
            try:
                send_telegram(text)
                state["last_error_notice"] = int(time.time())
                changed = True
                print("Error notice sent to Telegram.")
            except Exception as e:
                print(f"Could not send error notice: {type(e).__name__}: {e}")
        else:
            print("Error notice suppressed (one per 6 h).")

    if new_signals:
        text = render_message(new_signals)
        if DRY_RUN:
            print("\n----- DRY RUN: message that would be sent -----\n" + text + "\n----- (no Telegram, no files written) -----")
        else:
            send_telegram(text)
            append_archive(text)
            log_signals(new_signals)
            now = int(time.time())
            for s in new_signals:
                state["alerted"][signal_id(s)] = now
            changed = True
            print(f"Alert sent: {len(new_signals)} new sweep(s). Logged -> {SIGNALS_LOG}, archived -> {ALERTS_ARCHIVE}")
    else:
        print("No new sweeps — nothing sent.")

    if changed and not DRY_RUN:
        save_state(state)
        print(f"State saved -> {STATE_FILE} ({len(state['alerted'])} alerted ids kept)")


if __name__ == "__main__":
    main()
