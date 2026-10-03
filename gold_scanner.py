"""
Gold Liquidity Sweep (SFP) Scanner — XAU/USD, 15 minutes to 1 day
------------------------------------------------------------------
Scans ONE instrument — spot gold (XAU/USD), the chart you see in MetaTrader 5
(Pepperstone) — on 15m / 1h / 4h / 1d candles every 15 minutes (GitHub Actions
cron) and sends a Telegram alert ONLY when a liquidity sweep / Swing Failure
Pattern fires. Detection is identical to sweep-scanner:

  * ATR-filtered ZigZag swings (a low/high only counts once price CLOSES away
    from it by ATR_MULT x ATR), see detect_pivots.
  * The nearest confirmed swing whose level is still intact (no close beyond
    it, and with REQUIRE_UNTAPPED no wick through it either).
  * Signal on a CLOSED candle:
      Bullish sweep : low  < swing_low  AND close > swing_low
      Bearish sweep : high > swing_high AND close < swing_high
    Entry = close, stop = the sweep wick, target = TARGET_R x risk, 0-100 score.

Candles are built the way MT5 draws them: every feed delivers 15m and 1h bars;
4h / 1d (and 30m, 2h, 6h, 12h) are aggregated here with the trading day
starting at 17:00 New York (Pepperstone's server is GMT+2/+3 following US
daylight saving, so server midnight == 17:00 New York), bars outside the gold
session (Sun 18:00 -> Fri 16:59 New York, minus the 17:00-17:59 daily break)
are dropped. 15m and 1h bars are hour-aligned on every feed, so they match too.

Feeds (FEED=auto tries them in this order and uses the first that answers):
  tradingview  PEPPERSTONE:XAUUSD from TradingView's chart websocket — literally
               the Pepperstone chart. No login/key (unofficial protocol).
  dukascopy    Dukascopy's public chart feed, spot XAU/USD bid candles. No key.
  bitget       Bitget XAUUSDT perpetual (crypto-exchange gold, ~spot +/- a few $).
  oanda        OANDA v20 (needs OANDA_TOKEN; accounts are region-restricted).

Robust to GitHub's cron drift: every run examines the last LOOKBACK closed
candles of each timeframe (not only the newest), and gold_state.json
(committed back) guarantees each sweep is alerted exactly once.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required). Optional: FEED, TV_SYMBOL,
OANDA_TOKEN, OANDA_ENV, OANDA_PRICE, TIMEFRAMES, LOOKBACK, ATR_MULT,
REQUIRE_UNTAPPED, TARGET_R, MIN_SCORE, DRY_RUN, STATE_FILE, SIGNALS_LOG,
ALERTS_ARCHIVE.
"""

import os
import re
import json
import time
import random
import string
import calendar
from collections import namedtuple

import requests

# ── Config ─────────────────────────────────────────────────────────
FEED       = os.environ.get("FEED", "auto").strip().lower()      # auto | tradingview | dukascopy | bitget | oanda
FEED_ORDER = ["tradingview", "dukascopy", "bitget"]              # tried in this order when FEED=auto
TV_SYMBOL  = os.environ.get("TV_SYMBOL", "PEPPERSTONE:XAUUSD").strip()
OANDA_ENV        = os.environ.get("OANDA_ENV", "practice").strip().lower()
OANDA_TOKEN      = os.environ.get("OANDA_TOKEN", "").strip()
OANDA_INSTRUMENT = os.environ.get("OANDA_INSTRUMENT", "XAU_USD").strip()
OANDA_PRICE      = (os.environ.get("OANDA_PRICE", "B").strip().upper()[:1] or "B")   # B bid (MT5 default) | M | A
BITGET_SYMBOL    = "XAUUSDT"
DAY_START_SEC    = 17 * 3600          # MT5 trading day starts 17:00 New York (= server midnight)

TIMEFRAMES = [t.strip() for t in os.environ.get("TIMEFRAMES", "15m,1h,4h,1d").split(",") if t.strip()]
LOOKBACK   = max(1, int(os.environ.get("LOOKBACK", "3")))   # closed candles examined per timeframe, per run
CANDLES    = 120                                              # candles of history per timeframe
ATR_PERIOD = 14                                               # ATR lookback used by the swing filter
ATR_MULT   = float(os.environ.get("ATR_MULT", "2.0"))        # close-reversal (in ATRs) that confirms a swing
REQUIRE_UNTAPPED = os.environ.get("REQUIRE_UNTAPPED", "1") != "0"   # only FIRST-time sweeps
TARGET_R   = float(os.environ.get("TARGET_R", "2"))          # reward multiple (stop = 1R)
MIN_SCORE  = int(os.environ.get("MIN_SCORE", "0"))           # opt-in quality filter (0 = keep all)
VOL_LOOKBACK = 20                                             # candles for the average-volume baseline
DRY_RUN    = os.environ.get("DRY_RUN", "0").strip() == "1"   # print the message, send nothing, write nothing

STATE_FILE     = os.environ.get("STATE_FILE", "gold_state.json")
SIGNALS_LOG    = os.environ.get("SIGNALS_LOG", "gold_signals.jsonl")
ALERTS_ARCHIVE = os.environ.get("ALERTS_ARCHIVE", "gold_alerts_archive.txt")
STATE_KEEP_DAYS     = 30            # forget alerted ids older than this
ERROR_NOTICE_EVERY  = 6 * 3600      # at most one "feed error" Telegram notice per 6 h
MARKET_CLOSED_AFTER = 2 * 3600      # newest 15m candle older than this → market closed (log only)

# timeframe -> (candle seconds, base series it is built from)
TF = {
    "15m": (900,   900),  "30m": (1800,  900),
    "1h":  (3600,  3600), "2h":  (7200,  3600), "4h": (14400, 3600),
    "6h":  (21600, 3600), "12h": (43200, 3600), "1d": (86400, 3600),
}
FEED_LABEL = {"tradingview": f"TradingView {TV_SYMBOL}", "dukascopy": "Dukascopy XAU/USD bid",
              "bitget": f"Bitget {BITGET_SYMBOL} perp",
              "oanda": f"OANDA {OANDA_INSTRUMENT.replace('_', '')} {dict(B='bid', M='mid', A='ask')[OANDA_PRICE]}"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# Parallel per-candle arrays (ascending). `opens` are candle start times in ms
# (each candle's id); `oprices` open prices; `vols` volume.
Klines = namedtuple("Klines", "highs lows closes opens oprices vols")

session = requests.Session()
session.headers.update({"User-Agent": "gold-sweep-scanner/1.0", "Accept": "application/json"})


def _klines(rows):
    """rows: ascending (ts_ms, o, h, l, c, v) -> Klines."""
    return Klines(highs=[r[2] for r in rows], lows=[r[3] for r in rows], closes=[r[4] for r in rows],
                  opens=[r[0] for r in rows], oprices=[r[1] for r in rows], vols=[r[5] for r in rows])


# ── New York session calendar (what MT5 / Pepperstone draws) ───────
def _nth_sunday(year, month, n):
    first = calendar.timegm((year, month, 1, 0, 0, 0))
    return first + (((6 - time.gmtime(first).tm_wday) % 7) + 7 * (n - 1)) * 86400


def ny_offset(ts):
    """UTC offset (s) of America/New_York at unix ts: EDT (-4h) from the 2nd Sunday
    of March 07:00 UTC to the 1st Sunday of November 06:00 UTC, else EST (-5h)."""
    y = time.gmtime(ts).tm_year
    if _nth_sunday(y, 3, 2) + 7 * 3600 <= ts < _nth_sunday(y, 11, 1) + 6 * 3600:
        return -4 * 3600
    return -5 * 3600


def in_session(ts):
    """Does a bar STARTING at unix ts lie inside the gold session —
    Sun 18:00 -> Fri 16:59 New York, minus the 17:00-17:59 daily break?"""
    t = time.gmtime(ts + ny_offset(ts))
    wd, hr = t.tm_wday, t.tm_hour
    if hr == 17 or wd == 5:
        return False
    if wd == 4 and hr > 17:
        return False
    if wd == 6 and hr < 18:
        return False
    return True


def _shifted(ts):
    """Seconds since the MT5 day origin (17:00 New York), in local time."""
    return ts + ny_offset(ts) - DAY_START_SEC


def _unshift(sh):
    """Inverse of _shifted: shifted-local seconds -> unix ts (two passes for DST)."""
    loc = sh + DAY_START_SEC
    utc = loc - ny_offset(loc + 4 * 3600)
    return loc - ny_offset(utc)


def aggregate(k, secs, now=None):
    """Build `secs`-long candles from base bars, bucketed from 17:00 New York —
    exactly how MT5 (server midnight = 17:00 NY) builds H2/H4/H6/H12/D1 bars.
    Drops the oldest bucket (may be partial) and any bucket that has not ended."""
    now = now or time.time()
    buckets, order = {}, []
    for i, ts_ms in enumerate(k.opens):
        sh = _shifted(ts_ms // 1000)
        b = (sh // 86400) * 86400 + ((sh % 86400) // secs) * secs
        if b not in buckets:
            buckets[b] = [k.oprices[i], k.highs[i], k.lows[i], k.closes[i], k.vols[i]]
            order.append(b)
        else:
            c = buckets[b]
            c[1] = max(c[1], k.highs[i]); c[2] = min(c[2], k.lows[i]); c[3] = k.closes[i]; c[4] += k.vols[i]
    rows = []
    for b in order[1:]:
        if _unshift(b + secs) > now:
            continue                                     # still forming
        o, h, l, c, v = buckets[b]
        rows.append((_unshift(b) * 1000, o, h, l, c, v))
    return _klines(rows)


def prepare(base, tf, now=None):
    """Closed, in-session candles for timeframe tf from the base series dict."""
    secs, base_secs = TF[tf]
    now = now or time.time()
    k = base[base_secs]
    rows = [(k.opens[i], k.oprices[i], k.highs[i], k.lows[i], k.closes[i], k.vols[i])
            for i in range(len(k.opens))
            if in_session(k.opens[i] // 1000) and k.opens[i] // 1000 + base_secs <= now]
    k = _klines(rows)
    if secs == base_secs:
        return k
    return aggregate(k, secs, now)


def bars_needed():
    """How many base bars each feed must deliver for CANDLES candles per timeframe."""
    need = {}
    for tf in TIMEFRAMES:
        secs, base_secs = TF[tf]
        n = (CANDLES + LOOKBACK + 2) * (secs // base_secs) + 24
        need[base_secs] = max(need.get(base_secs, 0), n)
    return need


# ── Feeds: each returns ascending base bars (forming bar may be included) ──
def tradingview_bars(base_secs, n):
    """TradingView chart websocket, no login (unofficial). Resolution 15 / 60."""
    import websocket                                     # websocket-client
    res = {900: "15", 3600: "60"}[base_secs]
    pack = lambda m, p: (lambda body: f"~m~{len(body)}~m~{body}")(json.dumps({"m": m, "p": p}, separators=(",", ":")))
    cs = "cs_" + "".join(random.choices(string.ascii_lowercase, k=12))
    ws = websocket.create_connection("wss://data.tradingview.com/socket.io/websocket?from=chart%2F&type=chart",
                                     header={"Origin": "https://www.tradingview.com", "User-Agent": BROWSER_UA},
                                     timeout=20)
    try:
        ws.send(pack("set_auth_token", ["unauthorized_user_token"]))
        ws.send(pack("chart_create_session", [cs, ""]))
        ws.send(pack("switch_timezone", [cs, "Etc/UTC"]))
        ws.send(pack("resolve_symbol", [cs, "sym_1", "=" + json.dumps({"symbol": TV_SYMBOL, "adjustment": "splits", "session": "regular"})]))
        ws.send(pack("create_series", [cs, "s1", "s1", "sym_1", res, min(n, 5000), ""]))
        bars, done, t0 = {}, False, time.time()
        while not done and time.time() - t0 < 40:
            raw = ws.recv()
            for part in re.split(r"~m~\d+~m~", raw):
                if not part:
                    continue
                if part.startswith("~h~"):
                    ws.send(f"~m~{len(part)}~m~{part}")   # heartbeat echo
                    continue
                try:
                    msg = json.loads(part)
                except ValueError:
                    continue
                m = msg.get("m")
                if m in ("timescale_update", "du"):
                    for b in (msg["p"][1].get("s1") or {}).get("s", []):
                        v = b["v"]
                        bars[int(v[0])] = (int(v[0]) * 1000, float(v[1]), float(v[2]), float(v[3]), float(v[4]),
                                           float(v[5]) if len(v) > 5 and v[5] is not None else 0.0)
                elif m == "series_completed":
                    done = True
                elif m in ("symbol_error", "series_error", "critical_error", "protocol_error"):
                    raise RuntimeError(f"TradingView {m}: {part[:160]}")
    finally:
        ws.close()
    if len(bars) < ATR_PERIOD + 10:
        raise RuntimeError(f"TradingView returned only {len(bars)} bars for {TV_SYMBOL} {res}")
    return _klines([bars[t] for t in sorted(bars)])


def dukascopy_bars(base_secs, n):
    """Dukascopy's public chart feed (JSONP, newest-first): [ts_ms, o, h, l, c, vol], bid side."""
    r = session.get("https://freeserv.dukascopy.com/2.0/",
                    params={"path": "chart/json3", "instrument": "XAU/USD", "offer_side": "B",
                            "interval": {900: "15MIN", 3600: "1HOUR"}[base_secs], "splits": "true",
                            "stocks": "true", "limit": min(n, 5000), "time_direction": "P",
                            "timestamp": int(time.time() * 1000), "jsonp": "_callbacks____x"},
                    headers={"User-Agent": BROWSER_UA, "Referer": "https://freeserv.dukascopy.com/2.0/",
                             "Origin": "https://freeserv.dukascopy.com"}, timeout=40)
    r.raise_for_status()
    m = re.search(r"\((.*)\)\s*;?\s*$", r.text, re.S)
    rows = json.loads(m.group(1)) if m else []
    rows = [x for x in (rows or []) if x]
    if len(rows) < ATR_PERIOD + 10:
        raise RuntimeError(f"Dukascopy returned only {len(rows)} bars")
    rows.sort(key=lambda x: x[0])
    return _klines([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows])


def bitget_bars(base_secs, n):
    """Bitget USDT-M perpetual, ascending [start_ms, o, h, l, c, baseVol, quoteVol];
    /candles gives <= 1000, older pages via /history-candles (200 each)."""
    gran = {900: "15m", 3600: "1H"}[base_secs]
    base = f"{BITGET_BASE}/api/v2/mix/market"
    r = session.get(f"{base}/candles", params={"symbol": BITGET_SYMBOL, "productType": "USDT-FUTURES",
                                               "granularity": gran, "limit": min(n, 1000)}, timeout=25)
    r.raise_for_status()
    rows = [tuple(x) for x in (r.json().get("data") or [])]
    pages = 0
    while len(rows) < n and pages < 10 and rows:
        oldest = min(int(x[0]) for x in rows)
        r = session.get(f"{base}/history-candles", params={"symbol": BITGET_SYMBOL, "productType": "USDT-FUTURES",
                                                           "granularity": gran, "limit": 200, "endTime": oldest}, timeout=25)
        r.raise_for_status()
        page = [tuple(x) for x in (r.json().get("data") or []) if int(x[0]) < oldest]
        if not page:
            break
        rows += page
        pages += 1
        time.sleep(0.1)
    rows.sort(key=lambda x: int(x[0]))
    if len(rows) < ATR_PERIOD + 10:
        raise RuntimeError(f"Bitget returned only {len(rows)} bars")
    return _klines([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows[-n:]])


BITGET_BASE = "https://api.bitget.com"


def oanda_bars(base_secs, n):
    """OANDA v20 M15 / H1 candles (hour-aligned; 4h/1d are built locally like for every feed)."""
    if not OANDA_TOKEN:
        raise RuntimeError("OANDA_TOKEN is not set")
    host = "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"
    r = session.get(f"{host}/v3/instruments/{OANDA_INSTRUMENT}/candles",
                    params={"granularity": {900: "M15", 3600: "H1"}[base_secs], "count": min(n, 5000),
                            "price": OANDA_PRICE, "smooth": "false"},
                    headers={"Authorization": f"Bearer {OANDA_TOKEN}", "Accept-Datetime-Format": "UNIX"}, timeout=25)
    r.raise_for_status()
    key = {"B": "bid", "M": "mid", "A": "ask"}[OANDA_PRICE]
    rows = [(int(float(c["time"]) * 1000), float(c[key]["o"]), float(c[key]["h"]), float(c[key]["l"]),
             float(c[key]["c"]), float(c.get("volume") or 0))
            for c in (r.json().get("candles") or []) if c.get("complete")]
    return _klines(rows)


FEEDS = {"tradingview": tradingview_bars, "dukascopy": dukascopy_bars, "bitget": bitget_bars, "oanda": oanda_bars}


def load_base(feed_name, need):
    """All base series for one feed, or raise."""
    return {secs: FEEDS[feed_name](secs, n) for secs, n in sorted(need.items())}


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
                sig.update({"timeframe": tf, "candle_ts": sub.opens[-1], "score": score, "reason": reason})
                out.append(sig)
    return out


def signal_id(s):
    return f"{s['timeframe']}:XAUUSD:{s['candle_ts']}"       # feed-independent: a failover never re-alerts


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


def log_signals(sigs, feed, path=SIGNALS_LOG):
    now = int(time.time())
    with open(path, "a", encoding="utf-8") as f:
        for s in sigs:
            row = {"id": signal_id(s), "ts": now, "feed": feed, "timeframe": s["timeframe"],
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


def render_message(sigs, feed):
    order = {tf: i for i, tf in enumerate(TIMEFRAMES)}
    sigs = sorted(sigs, key=lambda s: (order.get(s["timeframe"], 99), s["candle_ts"]))
    bulls = [s for s in sigs if s["direction"] == "bull"]
    bears = [s for s in sigs if s["direction"] == "bear"]
    parts = [f"🥇 Gold sweep — XAUUSD ({FEED_LABEL[feed]}) — {time.strftime('%d %b %Y %H:%M', time.gmtime())} UTC"]
    if bears:
        parts.append("🔴 Bearish sweep" + ("s" if len(bears) > 1 else "") + ":\n" + "\n".join(fmt_line(s) for s in bears))
    if bulls:
        parts.append("🟢 Bullish sweep" + ("s" if len(bulls) > 1 else "") + ":\n" + "\n".join(fmt_line(s) for s in bulls))
    return "\n\n".join(parts)


# ── Main ───────────────────────────────────────────────────────────
def main():
    bad = [t for t in TIMEFRAMES if t not in TF]
    if bad or (FEED != "auto" and FEED not in FEEDS):
        raise SystemExit(f"Bad config: FEED={FEED} TIMEFRAMES={TIMEFRAMES} (unknown: {bad})")
    print(f"Gold sweep scan | feed={FEED} | tfs={','.join(TIMEFRAMES)} | lookback={LOOKBACK}"
          f" | atr_mult={ATR_MULT} untapped={REQUIRE_UNTAPPED} target={TARGET_R}R min_score={MIN_SCORE}"
          f" | dry_run={DRY_RUN}")
    need = bars_needed()
    state = load_state()

    # 1. fetch base series from the first feed that answers
    base, feed, errors = None, None, []
    for name in (FEED_ORDER if FEED == "auto" else [FEED]):
        try:
            t0 = time.time()
            base = load_base(name, need)
            feed = name
            print(f"[feed] {FEED_LABEL[name]}: " + ", ".join(f"{n} x {s // 60}m" for s, n in
                  sorted((s, len(k.opens)) for s, k in base.items())) + f" in {time.time() - t0:.1f}s")
            break
        except Exception as e:
            msg = f"{name}: {type(e).__name__}: {str(e)[:140]}".replace("\n", " ")
            errors.append(msg)
            print(f"[feed] {msg}")

    # 2. scan every timeframe
    new_signals, newest_15m = [], None
    if base:
        for tf in TIMEFRAMES:
            k = prepare(base, tf)
            k = Klines(*(a[-(CANDLES + LOOKBACK):] for a in k))   # same history window as sweep-scanner
            if len(k.closes) < ATR_PERIOD + 10:
                print(f"[{tf}] only {len(k.closes)} candles — too few to scan")
                continue
            sigs = scan_timeframe(tf, k)
            fresh = [s for s in sigs if signal_id(s) not in state["alerted"]]
            new_signals += fresh
            if tf == "15m":
                newest_15m = k.opens[-1] / 1000 + 900
            print(f"[{tf}] {len(k.closes)} candles · last closed {fmt_ts(k.opens[-1])} close {fmt_price(k.closes[-1])}"
                  f" · {len(sigs)} sweep(s) in last {LOOKBACK} · {len(fresh)} new")
            for s in sigs:
                tag = "NEW" if s in fresh else "already alerted"
                print(f"      {s['direction']:4} candle {fmt_ts(s['candle_ts'])} level {fmt_price(s['level'])} "
                      f"entry {fmt_price(s['entry'])} stop {fmt_price(s['stop'])} target {fmt_price(s['target'])} "
                      f"score {s['score']} ({s['reason']}) — {tag}")
        if newest_15m and time.time() - newest_15m > MARKET_CLOSED_AFTER:
            print(f"Market looks closed (newest 15m candle closed {(time.time() - newest_15m) / 3600:.1f} h ago).")

    # 3. notify
    changed = False
    if base is None:
        text = "⚠️ Gold scanner — every feed failed\n" + "\n".join(errors)
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
        text = render_message(new_signals, feed)
        if DRY_RUN:
            print("\n----- DRY RUN: message that would be sent -----\n" + text + "\n----- (no Telegram, no files written) -----")
        else:
            send_telegram(text)
            append_archive(text)
            log_signals(new_signals, feed)
            now = int(time.time())
            for s in new_signals:
                state["alerted"][signal_id(s)] = now
            changed = True
            print(f"Alert sent: {len(new_signals)} new sweep(s). Logged -> {SIGNALS_LOG}, archived -> {ALERTS_ARCHIVE}")
    elif base is not None:
        print("No new sweeps — nothing sent.")

    if changed and not DRY_RUN:
        save_state(state)
        print(f"State saved -> {STATE_FILE} ({len(state['alerted'])} alerted ids kept)")


if __name__ == "__main__":
    main()
