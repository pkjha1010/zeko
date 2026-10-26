"""
SHAPELIVE PLAN  -  BPB (breakout short) ONLY.  Run it ONCE per trading day shortly BEFORE 12:00 IST (the workflow starts it ~11:45; it waits for 12:00:00).
  At 12:00:00 it creates the Kite ATO alerts for TODAY.  Only setups whose ENTRY PRICE HAS NOT BEEN TOUCHED since the market opened (09:15) get an alert:
  SHORT setup -> the stock's day low so far must still be ABOVE the entry level (and the last price too).  Entries 12:00 - 15:29, time exit 15:30.

BPB  BREAKOUT_BPB  SHORT (buy PUT)   breakout -> pullback -> re-cross of the 10-day low (bpb_fno_backtest.py state machine).
                                     No stop-loss, target +60 %.  At most 2 trades open at the same time (the manager enforces it).  Max 15 % of the funds per trade.
(WRU / WRL are still in the code - STRATEGIES=BPB,WRU adds them - but only BPB is planned by default.)

What it does
  1. Gets daily candles of the F&O stocks from ANGEL ONE (SmartAPI) and finds the BPB setups (completed daily candles up to yesterday).
  2. Picks the PUT at the first strike AT OR BELOW the entry level, nearest MONTHLY expiry; reads its price from Angel One.
  3. WAITS until ALERT_AT (12:00:00).  Then reads the day low / last price of every setup stock from Kite and DROPS every setup whose entry level was already touched since 09:15.
  4. lots = cap // (price x lot size), cap = the strategy's own cap_pct (7 % for both WRU and BPB; env WRU_CAPITAL_PCT / BPB_CAPITAL_PCT) of the funds available in Kite (or TOTAL_FUNDS / MAX_COST).  Fewer than 1 lot -> SKIPPED.  Price at or below MIN_PRICE (Rs 5) -> SKIPPED.
  5. One Kite ATO alert per remaining setup: when the STOCK price crosses the entry level (<= for SHORT) Zerodha sends BUY MARKET for the option (NRML, DAY) with the built-in GTT target +60 %.
     Alert names: SHP|BPB|STOCK|price.  Orders carry the tag SHPBPB.
  6. Saves live_plan/plan.json (a dry run saves live_plan/preview.json).  The manager (next step of the same workflow run) reads it.
Only alerts whose name starts with "SHP|" are touched. Your other alerts (SWG|...) are not.
"""
import os, io, sys, json, time, requests, pyotp, numpy as np, pandas as pd
from datetime import datetime, date, timedelta, time as dtime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect

PFX = "SHP|"
STRATS = {   # code: rules.  entry_end = the alert is deleted / waiting BUYs are cancelled from this second on; exit_at = square-off second.
    "WRU": dict(name="WICK_RED_UPPER", direction="SHORT", kind="SHAPE", red=True, lower_longer=False, entry_end="09:30:00", exit_at="09:30:00", sl_pct=20.0, tp_pct=0.0, max_open=0, cap_pct=7.0, check_untouched=False),
    "WRL": dict(name="WICK_RED_LOWER", direction="LONG", kind="SHAPE", red=True, lower_longer=True, entry_end="10:00:00", exit_at="10:00:00", sl_pct=0.0, tp_pct=60.0, max_open=0, cap_pct=7.0, check_untouched=False),
    "BPB": dict(name="BREAKOUT_BPB", direction="SHORT", kind="BPB", entry_end="15:29:00", exit_at="15:30:00", sl_pct=0.0, tp_pct=60.0, max_open=2, cap_pct=7.0, check_untouched=True),   # cap_pct = % of the Kite funds one trade may use; check_untouched = drop setups touched since 09:15 at ALERT_AT. max_open = positions open at the same time (0 = no limit)
}
for _c, _r in STRATS.items():                                   # env overrides, e.g. WRU_TP=60  WRU_SL=20  BPB_EXIT_AT=15:30:00  WRL_ENTRY_END=10:00:00
    for _k, _t in (("tp_pct", "TP"), ("sl_pct", "SL")):
        if os.environ.get(f"{_c}_{_t}"): _r[_k] = float(os.environ[f"{_c}_{_t}"])
    if os.environ.get(f"{_c}_MAX_OPEN"): _r["max_open"] = int(os.environ[f"{_c}_MAX_OPEN"])
    if os.environ.get(f"{_c}_CAPITAL_PCT"): _r["cap_pct"] = float(os.environ[f"{_c}_CAPITAL_PCT"])         # e.g. WRU_CAPITAL_PCT=7  BPB_CAPITAL_PCT=15
    for _k in ("entry_end", "exit_at"):
        if os.environ.get(f"{_c}_{_k.upper()}"): _r[_k] = os.environ[f"{_c}_{_k.upper()}"]
ORDER = ["WRU", "WRL", "BPB"]                                  # priority when two strategies pick the same option
ACTIVE = [c for c in ORDER if c in {x.strip().upper() for x in (os.environ.get("STRATEGIES") or "BPB").replace(" ", "").split(",") if x.strip()}]
BPB_LOOKBACK = int(os.environ.get("BPB_LOOKBACK") or 10)       # rolling-high window of the breakout strategy (days), same as the backtest
CAPITAL_PCT = float(os.environ.get("CAPITAL_PCT") or 7)        # fallback % per trade for a strategy without its own cap_pct (7 % for both WRU and BPB - see STRATS)
MIN_PRICE = float(os.environ.get("MIN_PRICE") or 5)            # options priced AT OR BELOW this (Rs, per unit) are not traded (default Rs 5: a Rs 5.00 option is excluded, Rs 5.05 is allowed)
MAX_COST_FIXED = float(os.environ.get("MAX_COST") or 0)        # OPTIONAL fixed rupee cap per trade instead of the percentage (0 = use CAPITAL_PCT of the funds)
TOTAL_FUNDS = float(os.environ.get("TOTAL_FUNDS") or 0)        # OPTIONAL: size the trades on this total (Rs) instead of the funds available in Kite at planning time
PRODUCT = os.environ.get("PRODUCT") or "NRML"                  # overnight product: this is what carries the built-in GTT
# basket params["gtt"] layout.  {tp} / {sl} are replaced by the percentages.  If probe mode shows your account stores GTT differently, set these two env values.
GTT_TP_ONLY = os.environ.get("GTT_TP_ONLY") or '{"target": {tp}, "stoploss": 0}'
GTT_TP_SL = os.environ.get("GTT_TP_SL") or '{"target": {tp}, "stoploss": -{sl}}'
GTT_SL_ONLY = os.environ.get("GTT_SL_ONLY") or '{"target": 0, "stoploss": -{sl}}'      # stop-loss without a target (your old plan used {"target": 0, "stoploss": -10})
DRY_RUN = (os.environ.get("DRY_RUN") or "0") == "1"           # 1 = show everything, create no alerts 
FORCE = (os.environ.get("FORCE") or "0") == "1"                 # 1 = allow a run during market hours (it then plans the next trading day)
PLAN_DIR = os.environ.get("PLAN_DIR") or "live_plan"
ALERT_AT = os.environ.get("ALERT_AT") or "12:00:00"           # the alerts are created at this second (after the untouched-since-open check)


def gtt_for(r):
    sl, tp = r["sl_pct"] > 0, r["tp_pct"] > 0
    t = GTT_TP_SL if (sl and tp) else GTT_SL_ONLY if sl else GTT_TP_ONLY
    return json.loads(t.replace("{tp}", f"{r['tp_pct']:g}").replace("{sl}", f"{r['sl_pct']:g}"))


UNIVERSE = """
    360ONE ABCAPITAL ADANIENSOL ABB ADANIPORTS AMBER ASIANPAINT AMBUJACEM
    ADANIENT BAJAJ-AUTO BAJAJHLDNG APLAPOLLO ADANIGREEN AUROPHARMA AXISBANK
    BOSCHLTD BRITANNIA CDSL BANDHANBNK BANKBARODA BDL BHARATFORG COALINDIA
    BHARTIARTL BLUESTARCO BPCL ALKEM CAMS CHOLAFIN CIPLA COCHINSHIP COLPAL
    COFORGE CROMPTON CONCOR DABUR ANGELONE APOLLOHOSP DELHIVERY DIVISLAB DLF
    DMART DRREDDY DIXON ETERNAL FORCEMOT FORTIS GLENMARK GODFRYPHLP BANKINDIA
    GODREJPROP GODREJCP HCLTECH HDFCBANK GRASIM GVT&D HAVELLS HDFCLIFE BEL
    BHEL HINDALCO BIOCON HINDUNILVR HINDZINC IDFCFIRSTB INDUSINDBK ICICIGI
    CGPOWER IRFC JINDALSTEL JIOFIN IEX INDHOTEL INDIANB JSWENERGY JSWSTEEL
    KAYNES KEI INFY INOXWIND ITC KPITTECH JUBLFOOD LT M&M KALYANKJIL MANKIND
    MARICO ADANIPOWER MCX NAM-INDIA KFINTECH NESTLEIND EICHERMOT LODHA
    FEDERALBNK OBEROIRLTY LTM LUPIN PAGEIND MANAPPURAM PAYTM PFC PGEL MARUTI
    MAXHEALTH PHOENIXLTD PNB MOTHERSON POLYCAB POWERINDIA NMDC OFSS PRESTIGE
    OIL ONGC PATANJALI RECLTD SBILIFE GMRAIRPORT SBIN SHREECEM TATACONSUM
    PETRONET TATAELXSI TATASTEEL TMPV UPL PIDILITIND WAAREEENER PIIND
    BAJAJFINSV POLICYBZR PREMIERENE RADICO RBLBANK SAIL SOLARINDS SUNPHARMA
    SUPREMEIND SUZLON SWIGGY TATAPOWER TCS TIINDIA TITAN TVSMOTOR UNIONBANK
    VBL VEDL ZYDUSLIFE HEROMOTOCO HYUNDAI ICICIBANK INDIGO BSE LICHSGFIN LTF
    HDFCAMC MOTILALOFS MPHASIS NATIONALUM NBCC NYKAA ICICIPRULI PERSISTENT
    PNBHOUSING POWERGRID LAURUSLABS RELIANCE MAZDOCK SONACOMS NAUKRI SRF TECHM
    ULTRACEMCO UNOMINDA VMM WIPRO ASTRAL BAJFINANCE UNITDSPR ASHOKLEY
    MUTHOOTFIN CUMMINSIND GAIL SIEMENS MFSL NHPC NTPC TORNTPHARM RVNL
    KOTAKBANK LICI AUBANK HAL IDEA INDUSTOWER IOC IREDA TRENT SBICARD
    SHRIRAMFIN CANBK YESBANK VOLTAS HINDPETRO ATHERENERG MAHABANK SAGILITY
""".split()

# ---------------------------------------------------------------- shared: Kite login + thin REST client
IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.kite.trade"


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)          # naive IST wall clock


def log(m):
    print(f"[{now_ist():%H:%M:%S.%f}"[:-3] + f"] {m}", flush=True)


def get_request_token(key, uid, pwd, totp_key):
    s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0"
    lu, params = "https://kite.zerodha.com/connect/login", {"v": "3", "api_key": key}
    s.get(lu, params=params, timeout=30)
    r = s.post("https://kite.zerodha.com/api/login", data={"user_id": uid, "password": pwd}, timeout=30)
    r.raise_for_status(); rid = r.json()["data"]["request_id"]
    r = s.post("https://kite.zerodha.com/api/twofa", timeout=30, data={
        "user_id": uid, "request_id": rid, "twofa_value": pyotp.TOTP(totp_key).now(), "twofa_type": "totp", "skip_session": "true"})
    r.raise_for_status()
    url, first = lu, True
    for _ in range(8):
        r = s.get(url, params=params if first else None, allow_redirects=False, timeout=30); first = False
        loc = r.headers.get("Location")
        if not loc: break
        url = urljoin(r.url, loc)
        qs = parse_qs(urlparse(url).query)
        if "request_token" in qs: return qs["request_token"][0]
    raise RuntimeError("request_token not found (open the Kite app login URL once in a browser and check the redirect URL)")


def login(tries=3):
    key, sec = os.environ["KITE_API_KEY"].strip(), os.environ["KITE_API_SECRET"].strip()
    uid, pwd = os.environ["ZERODHA_USER_ID"].strip(), os.environ["ZERODHA_PASSWORD"].strip()
    tk = os.environ["ZERODHA_TOTP_KEY"].strip().replace(" ", "")
    last = None
    for i in range(1, tries + 1):
        try:
            kite = KiteConnect(api_key=key, timeout=60)           # kiteconnect's default read timeout is 7 s - too short for the big instruments file
            d = kite.generate_session(get_request_token(key, uid, pwd, tk), api_secret=sec)
            kite.set_access_token(d["access_token"])
            log("Kite login OK"); return kite, key, d["access_token"]
        except Exception as e:
            last = e; log(f"login attempt {i}/{tries} failed: {e}"); time.sleep(5)
    raise last


def kite_instruments(kite, tries=5):
    """NFO instrument dump (a multi-MB CSV).  A single read timeout used to kill the whole plan after the 10+ minute Angel scan, so: long timeout (see login) + retries."""
    last = None
    for i in range(1, tries + 1):
        try:
            d = kite.instruments("NFO"); log(f"Kite NFO instruments OK ({len(d)} rows)"); return d
        except Exception as e:
            last = e; log(f"Kite instruments attempt {i}/{tries} failed: {repr(e)[:200]}"); time.sleep(min(30, 4 * i))
    sys.exit(f"Kite NFO instruments could not be downloaded after {tries} attempts: {repr(last)[:200]}")


class K:
    """Kite REST with one persistent connection (keep-alive = no new TLS handshake per order)."""
    def __init__(self, key, token):
        self.s = requests.Session()
        self.s.headers.update({"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
        self.s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))

    def call(self, method, path, tries=1, **kw):
        last = None
        for i in range(tries):
            try:
                r = self.s.request(method, BASE + path, timeout=10, **kw)
                try: j = r.json()
                except Exception: j = {"status": "error", "message": r.text[:200]}
                if r.status_code == 200 and j.get("status") == "success": return True, j.get("data")
                last = j.get("message") or str(j)[:200]
                if r.status_code in (400, 403): break          # rejected - retrying will not help
            except Exception as e:
                last = str(e)
            time.sleep(0.15)
        return False, last


# ---------------------------------------------------------------- setup logic (same as swing_alerts.py)
def shape(o, h, l, c):
    """Last candle: body < 45% of range and a wick (upper or lower) >= 2x body."""
    if h == l: return False
    b = abs(c - o)
    return b / (h - l) < .45 and max(h - max(o, c), min(o, c) - l) >= 2 * b


def setup(df, long):
    """Most recent valid setup for the last candle, else None -> (ref_line, swing_date, bos_date, pullback_date)."""
    n, res = len(df) - 1, None
    hi, lo, c = df.High.values, df.Low.values, df.Close.values
    for i in range(10, n):
        w = (hi if long else lo)[i - 10:i]
        k = w.argmax() if long else w.argmin()
        ref, sw = w[k], i - 10 + k
        if not (c[i] > ref if long else c[i] < ref): continue
        if i - sw > 20 or n - i > 20: continue
        if not (c[n] < ref if long else c[n] > ref): continue
        pb = next(j for j in range(i + 1, n + 1) if (c[j] < ref if long else c[j] > ref))
        res = (ref, df.index[sw], df.index[i], df.index[pb])
    return res


# ---------------------------------------------------------------- Angel One (candles + option prices)
A_BASE = "https://apiconnect.angelone.in"
A_MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


class Angel:
    def __init__(self):
        self.key = os.environ["ANGEL_API_KEY"].strip()
        self.h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
                  "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.key}
        last = None
        for i in range(3):
            try:
                r = requests.post(A_BASE + "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self.h, timeout=30, json={
                    "clientcode": os.environ["ANGEL_CLIENT_ID"].strip(), "password": os.environ["ANGEL_PIN"].strip(),
                    "totp": pyotp.TOTP(os.environ["ANGEL_TOTP_KEY"].strip().replace(" ", "")).now()})
                j = r.json()
                if j.get("status"): self.h["Authorization"] = "Bearer " + j["data"]["jwtToken"]; log("Angel login OK"); return
                last = f"{j.get('message')} ({j.get('errorcode')})"
            except Exception as e: last = repr(e)
            time.sleep(4)
        sys.exit(f"Angel login failed: {last}")

    def post(self, path, body, tries=4):
        for i in range(tries):
            try:
                r = requests.post(A_BASE + path, headers=self.h, json=body, timeout=40); j = r.json()
                if j.get("status"): return j.get("data")
                msg = str(j.get("message", ""))
                if "access rate" in msg.lower() or r.status_code == 429: time.sleep(1.5 * (i + 1)); continue
                return None
            except Exception: time.sleep(1)
        return None

    def daily(self, token, frm, to):
        d = self.post("/rest/secure/angelbroking/historical/v1/getCandleData", {
            "exchange": "NSE", "symboltoken": token, "interval": "ONE_DAY", "fromdate": f"{frm:%Y-%m-%d} 09:15", "todate": f"{to:%Y-%m-%d} 15:30"})
        return d or []

    def ltp(self, tokens):
        """{token: last price} for NFO tokens, 50 per request."""
        out = {}
        for i in range(0, len(tokens), 50):
            d = self.post("/rest/secure/angelbroking/market/v1/quote/", {"mode": "LTP", "exchangeTokens": {"NFO": tokens[i:i + 50]}})
            for x in (d or {}).get("fetched", []) or []: out[str(x.get("symbolToken"))] = float(x.get("ltp") or 0)
            time.sleep(1.1)
        return out

    def day_quotes(self, tokens):
        """{NSE equity token: dict(ltp, low, high)} - today's low / high so far and the last price, from Angel One (Kite's /quote needs a paid data plan)."""
        out = {}
        for i in range(0, len(tokens), 50):
            d = self.post("/rest/secure/angelbroking/market/v1/quote/", {"mode": "OHLC", "exchangeTokens": {"NSE": tokens[i:i + 50]}})
            for x in (d or {}).get("fetched", []) or []:
                out[str(x.get("symbolToken"))] = dict(ltp=float(x.get("ltp") or 0), low=float(x.get("low") or 0), high=float(x.get("high") or 0))
            time.sleep(1.1)
        return out


def angel_master():
    m, last = None, None
    for i in range(1, 7):                                                # the big file is sometimes cut off by the server - retry
        try:
            r = requests.get(A_MASTER, timeout=(20, 90), headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status(); m = r.json(); break
        except Exception as e:
            last = repr(e)[:160]; log(f"Angel contract list attempt {i}/6 failed: {last}"); time.sleep(5 * i)
    if m is None: sys.exit(f"Angel contract list could not be downloaded: {last}")
    log(f"Angel contract list OK ({len(m)} rows)")
    eq, opt = {}, {}
    for x in m:
        if x.get("exch_seg") == "NSE" and str(x.get("symbol", "")).endswith("-EQ"): eq[x["symbol"][:-3]] = x["token"]
        elif x.get("exch_seg") == "NFO" and x.get("instrumenttype") == "OPTSTK":
            try: e = datetime.strptime(x["expiry"], "%d%b%Y").date()
            except Exception: continue
            opt[(x["name"], e, round(float(x["strike"]) / 100, 2), "CE" if x["symbol"].endswith("CE") else "PE")] = x["token"]
    return eq, opt


# ---------------------------------------------------------------- setup logic (same as swing_alerts.py)
def shape(o, h, l, c):
    """Last candle: body < 45% of range and a wick (upper or lower) >= 2x body."""
    if h == l: return False
    b = abs(c - o)
    return b / (h - l) < .45 and max(h - max(o, c), min(o, c) - l) >= 2 * b


def setup(df, long):
    """Most recent valid setup for the last candle, else None -> (ref_line, swing_date, bos_date, pullback_date)."""
    n, res = len(df) - 1, None
    hi, lo, c = df.High.values, df.Low.values, df.Close.values
    for i in range(10, n):
        w = (hi if long else lo)[i - 10:i]
        k = w.argmax() if long else w.argmin()
        ref, sw = w[k], i - 10 + k
        if not (c[i] > ref if long else c[i] < ref): continue
        if i - sw > 20 or n - i > 20: continue
        if not (c[n] < ref if long else c[n] > ref): continue
        pb = next(j for j in range(i + 1, n + 1) if (c[j] < ref if long else c[j] > ref))
        res = (ref, df.index[sw], df.index[i], df.index[pb])
    return res


def breakout_armed(df, long, lookback=BPB_LOOKBACK):
    """BPB (breakout -> pullback -> re-cross) - the SAME state machine as find_setups() of bpb_fno_backtest.py, run over the completed daily candles.
    Returns (line, entry level) when a breakout has been confirmed by a pullback and price has NOT re-crossed the entry level yet, i.e. the next
    trading day may be the entry day; otherwise None.  SHORT is the mirror (negated prices, negated back).
      1. breakout    a close above the highest High of the previous `lookback` days (= the LINE)
      2. entry level the highest High since the breakout day (breakout day included)
      3. pullback    every later close must stay at or above the LINE (a close below it kills the setup)
      4. confirmed   after at least one bearish candle (close < open) following the breakout day
      5. entry       a later day on which price reaches the entry level (one setup = one entry day)"""
    O, H, L, C = (df[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
    if not long: O, H, L, C = -O, -L, -H, -C
    n = len(C)
    ref = np.full(n, np.nan)
    for i in range(lookback, n): ref[i] = H[i - lookback:i].max()
    st = None                                                          # [line, highest high since breakout, bearish seen]
    for i in range(lookback, n):
        if st is not None and st[2] and H[i] >= st[1]: st = None       # confirmed pullback and price re-crossed that day -> setup used up
        elif st is not None and st[2]:
            if C[i] < st[0]: st = None                                 # pullback candle closed below the line -> invalid
            continue
        elif st is not None:                                           # breakout seen, pullback not yet confirmed
            if C[i] < st[0]: st = None                                 # closed below the line -> invalid (a new setup may start below)
            else:
                st[1] = max(st[1], H[i])
                if C[i] < O[i]: st[2] = True                           # bearish candle after the breakout = pullback confirmed
                continue
        if st is None and C[i] > ref[i]: st = [ref[i], H[i], False]    # a new breakout day
    if st is None or not st[2]: return None
    sg = 1.0 if long else -1.0
    return sg * st[0], sg * st[1]


def candle_pattern(o, h, l, c):
    """Signal candle (the last completed day): red / green and which wick is longer (same definition as the backtest)."""
    up, lo = h - max(o, c), min(o, c) - l
    return dict(red=c < o, green=c > o, upper_longer=up > lo, lower_longer=lo > up)


def find_setups(ang, eq):
    out, have = [], 0
    to = now_ist(); frm = to - timedelta(days=130)
    for s in UNIVERSE:
        tok = eq.get(s)
        if not tok: log(f"{s}: not in the Angel contract list"); continue
        rows = ang.daily(tok, frm, to)
        time.sleep(0.4)                                                  # Angel candle limit is 3 requests/second
        if not rows: continue
        df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "V"])
        df.index = pd.to_datetime(df["t"].astype(str).str[:10])
        df = df[["Open", "High", "Low", "Close"]].astype(float).dropna()
        if len(df) and df.index[-1].date() == to.date() and to.time() < datetime(2000, 1, 1, 15, 45).time():
            df = df.iloc[:-1]                                            # today's candle still forming
        if len(df) < 40: continue
        have += 1
        o, h, l, c = df.iloc[-1][["Open", "High", "Low", "Close"]]
        pat = candle_pattern(o, h, l, c)
        for code in ACTIVE:
            r = STRATS[code]; long = r["direction"] == "LONG"
            if r["kind"] == "SHAPE":
                if not pat["red"] or pat["lower_longer"] != r["lower_longer"] or (not r["lower_longer"] and not pat["upper_longer"]): continue
                x = setup(df, long)
                if x: out.append(dict(stock=s, direction=r["direction"], ref=round(float(x[0]), 2), line=round(float(x[0]), 2), strategy=code, last=str(df.index[-1].date())))
            else:
                x = breakout_armed(df, long)
                if x and (c < x[1] if long else c > x[1]):             # last close must still be on the near side, or the alert would fire at once
                    out.append(dict(stock=s, direction=r["direction"], ref=round(float(x[1]), 2), line=round(float(x[0]), 2), strategy=code, last=str(df.index[-1].date())))
    cnt = {c: sum(1 for x in out if x["strategy"] == c) for c in ACTIVE}
    log(f"candles for {have}/{len(UNIVERSE)} stocks, {len(out)} setups: " + ", ".join(f"{STRATS[c]['name']} {n}" for c, n in cnt.items()))
    if have < 150: sys.exit("Candle download incomplete - alerts left untouched")
    out.sort(key=lambda x: ORDER.index(x["strategy"]))                 # priority order for option collisions
    return out


# ---------------------------------------------------------------- option choice
def monthly_expiries(exps):
    by = {}
    for e in sorted(set(exps)): by[(e.year, e.month)] = e            # last expiry of each month = monthly
    return sorted(by.values())


def next_trade_day(d):
    d += timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return d


def pick_options(setups, instruments, trade_day):
    byname = {}
    for i in instruments:
        if i.get("segment") == "NFO-OPT" and i.get("instrument_type") in ("CE", "PE"): byname.setdefault(i["name"], []).append(i)
    picks, skipped = [], []
    for sd in setups:
        lst = byname.get(sd["stock"])
        if not lst: skipped.append((sd, "no option contracts")); continue
        mexp = [e for e in monthly_expiries([i["expiry"] for i in lst]) if e >= trade_day]
        if not mexp: skipped.append((sd, "no monthly expiry left")); continue
        typ = "CE" if sd["direction"] == "LONG" else "PE"
        cands = [i for i in lst if i["expiry"] == mexp[0] and i["instrument_type"] == typ]
        if not cands: skipped.append((sd, "no strikes")); continue
        if sd["direction"] == "LONG":                                  # next strike at or above the entry price
            up = [i for i in cands if i["strike"] >= sd["ref"] - 1e-9]
            best = min(up, key=lambda i: i["strike"]) if up else max(cands, key=lambda i: i["strike"])
        else:                                                          # next strike at or below the entry price
            dn = [i for i in cands if i["strike"] <= sd["ref"] + 1e-9]
            best = max(dn, key=lambda i: i["strike"]) if dn else min(cands, key=lambda i: i["strike"])
        picks.append((sd, best))
    return picks, skipped


def option_prices(ang, opt, picks):
    """{kite tradingsymbol: last price} from Angel One. Kite contract -> Angel contract by (underlying, expiry, strike, CE/PE)."""
    want = {}
    for sd, i in picks:
        t = opt.get((sd["stock"], i["expiry"], round(float(i["strike"]), 2), i["instrument_type"]))
        if t: want[str(t)] = i["tradingsymbol"]
        else: log(f"no Angel contract for {i['tradingsymbol']}")
    got = ang.ltp(list(want))
    out = {want[t]: px for t, px in got.items() if t in want and px > 0}
    log(f"option prices found for {len(out)}/{len(picks)} contracts")
    return out


def basket(sym, qty, label, code):
    r = STRATS[code]
    return {"name": label, "type": "alert", "tags": [], "items": [{
        "type": "insert", "tradingsymbol": sym, "exchange": "NFO", "weight": 10000,
        "params": {"transaction_type": "BUY", "product": PRODUCT, "order_type": "MARKET", "validity": "DAY", "validity_ttl": 1,
                   "quantity": qty, "price": 0, "trigger_price": 0, "disclosed_quantity": 0, "last_price": 0, "variety": "regular",
                   "tags": ["SHP" + code], "gtt": gtt_for(r), "squareoff": 0, "stoploss": 0, "trailing_stoploss": 0, "iceberg_legs": 0, "market_protection": -1}}]}


def list_my_alerts(k):
    have, p = {}, 1
    while p < 30:
        ok, rows = k.call("GET", "/alerts", tries=3, params={"page": p, "page_size": 100})
        if not ok: sys.exit(f"cannot list alerts: {rows}")
        rows = rows or []
        have.update({a["uuid"]: a for a in rows})
        if len(rows) < 100: break
        p += 1
    return [a for a in have.values() if str(a.get("name", "")).startswith(PFX) and a.get("status") != "deleted"]


def funds_available(k):
    """Cash available for buying options in the Kite account (live balance)."""
    ok, d = k.call("GET", "/user/margins/equity", tries=3)
    if not ok or not d: return None
    av = d.get("available") or {}
    v = av.get("live_balance")
    if v is None: v = d.get("net")
    return float(v) if v is not None else None


def plan_day(n):
    """Always TODAY.  Weekend or after 15:00 -> refused (unless FORCE)."""
    today, t = n.date(), n.time()
    if FORCE: return today
    if today.weekday() >= 5: sys.exit("Weekend - no trading, nothing planned.")
    if t >= dtime(15, 0): sys.exit(f"It is {t:%H:%M}: too late to plan today's entries (BPB entries stop at 15:29). Nothing planned.")
    return today


def sleep_until(hms):
    h, m, *s = [int(x) for x in hms.split(":")]
    target = dtime(h, m, s[0] if s else 0)
    while now_ist().time() < target:
        time.sleep(min(0.5, max(0.01, (datetime.combine(now_ist().date(), target) - now_ist()).total_seconds())))


def market_open_today(k):
    ok, d = k.call("GET", "/quote", tries=3, params=[("i", "NSE:NIFTY 50")])
    if not ok or not d: return True                                      # unknown: assume open
    v = str(((d.get("NSE:NIFTY 50") or {}).get("last_trade_time")) or "")[:10]
    return v == str(now_ist().date())


def untouched(ang, eq, setups):
    """Keep only the setups whose entry level has NOT been touched since the open.  SHORT: day low so far > entry level AND last price > entry level.
    LONG (only if WRL/WRU-type strategies are enabled): day high < entry level AND last price < entry level.  No quote -> the setup is dropped (safe side).
    Quotes come from Angel One (the Kite quote call needs a paid Kite Connect data plan)."""
    keep = []
    stocks = sorted({s["stock"] for s in setups})
    toks = {s: str(eq[s]) for s in stocks if eq.get(s)}
    q_by_tok = ang.day_quotes(list(toks.values()))
    if not q_by_tok: sys.exit("Cannot read the stock quotes from Angel One - no alerts created")
    quotes = {s: q_by_tok.get(t) or {} for s, t in toks.items()}
    for sd in setups:
        q = quotes.get(sd["stock"]) or {}
        last, lo, hi = float(q.get("ltp") or 0), float(q.get("low") or 0), float(q.get("high") or 0)
        if last <= 0 or lo <= 0 or hi <= 0: log(f"{sd['stock']}: no quote -> dropped"); continue
        if sd["direction"] == "SHORT": touched = lo <= sd["ref"] or last <= sd["ref"]
        else: touched = hi >= sd["ref"] or last >= sd["ref"]
        if touched: log(f"{sd['stock']}: entry level {sd['ref']} already touched today (day low {lo}, high {hi}, last {last}) -> dropped"); continue
        keep.append(sd)
    log(f"entry level untouched since the open: {len(keep)} of {len(setups)} setups kept")
    return keep


def main():
    global DRY_RUN
    n = now_ist(); today = n.date()
    td = plan_day(n)
    log(f"planning trade day {td}" + ("  [dry run: no alerts created, plan.json untouched]" if DRY_RUN else "  (BPB alerts are created at " + ALERT_AT + ", morning strategies at once; plan.json is saved)"))
    kite, key, tok = login(); k = K(key, tok)
    os.makedirs(PLAN_DIR, exist_ok=True)
    if not FORCE and not market_open_today(k):
        log("No trading today (NIFTY has no trade yet) - empty plan, nothing created")
        json.dump(dict(created=str(now_ist()), trade_day=str(td), funds=None, max_cost=0, product=PRODUCT, strategies={c: STRATS[c] for c in ACTIVE}, items=[]), open(f"{PLAN_DIR}/{'preview' if DRY_RUN else 'plan'}.json", "w"), indent=1)
        sys.exit(0)
    ang = Angel(); eq, opt = angel_master()
    inst = kite_instruments(kite)                                      # fetched FIRST: if Kite is slow we know in seconds, not after the 10+ minute candle scan
    setups = find_setups(ang, eq)
    picks, skipped = pick_options(setups, inst, td)
    px = option_prices(ang, opt, picks)                                # everything slow is done BEFORE the alerts are created
    chk = [(sd, i) for sd, i in picks if STRATS[sd["strategy"]].get("check_untouched")]       # BPB: only setups whose level was not touched since the open
    if chk:
        log(f"waiting for {ALERT_AT} ...")
        sleep_until(ALERT_AT)
        left = untouched(ang, eq, [sd for sd, i in chk])                   # the check is made at ALERT_AT (12:00:00) - as late as possible
        ok_ids = {id(s) for s in left}
        for sd, i in chk:
            if id(sd) not in ok_ids: skipped.append((sd, "entry level touched since the open (or no quote)"))
        picks = [(sd, i) for sd, i in picks if not STRATS[sd["strategy"]].get("check_untouched") or id(sd) in ok_ids]
    else: log("no strategy needs the untouched-since-open check (morning strategies): alerts are created now")
    funds, caps = None, {}
    if MAX_COST_FIXED > 0:
        caps = {c: MAX_COST_FIXED for c in ACTIVE}; log(f"capital per trade: fixed Rs {MAX_COST_FIXED:,.0f}")
    else:
        funds = TOTAL_FUNDS if TOTAL_FUNDS > 0 else funds_available(k)
        if not funds or funds <= 0: sys.exit("Cannot read the available funds from Kite - nothing placed (set MAX_COST to use a fixed amount)")
        for c in ACTIVE:
            pct = float(STRATS[c].get("cap_pct") or CAPITAL_PCT); caps[c] = round(funds * pct / 100, 2)
            log(f"{'total funds' if TOTAL_FUNDS > 0 else 'available funds'} Rs {funds:,.0f} -> {STRATS[c]['name']}: capital per trade {pct:g}% = Rs {caps[c]:,.0f}")
    cap = max(caps.values()) if caps else 0
    items, total, taken = [], 0.0, {}
    for sd, i in picks:                                                # picks are in strategy priority order (WRU, WRL, BPB)
        p = px.get(i["tradingsymbol"]); lot = int(i["lot_size"])
        if not p: skipped.append((sd, f"{i['tradingsymbol']}: no price")); continue
        if p <= MIN_PRICE: skipped.append((sd, f"{i['tradingsymbol']}: price Rs {p:.2f} is at or below the Rs {MIN_PRICE:g} limit")); continue
        scap = caps[sd["strategy"]]
        lots = int(scap // (p * lot))
        if lots < 1: skipped.append((sd, f"{i['tradingsymbol']}: 1 lot costs Rs {p * lot:,.0f} > cap Rs {scap:,.0f}")); continue
        if i["tradingsymbol"] in taken: skipped.append((sd, f"{i['tradingsymbol']}: already planned by {taken[i['tradingsymbol']]} (one option = one strategy)")); continue
        taken[i["tradingsymbol"]] = sd["strategy"]
        total += lots * lot * p
        items.append(dict(stock=sd["stock"], direction=sd["direction"], strategy=sd["strategy"], ref=sd["ref"], line=sd["line"], option=i["tradingsymbol"], strike=i["strike"],
                          expiry=str(i["expiry"]), lot_size=lot, lots=lots, qty=lots * lot, close_price=p, cost=round(lots * lot * p, 2)))
    log(f"total cost of all planned orders Rs {total:,.0f}")
    log(f"trade day {td}: {len(setups)} setups ({len(picks)} untouched) -> {len(items)} orders within the per-trade cap ({', '.join(f'{c} Rs {v:,.0f}' for c, v in caps.items())}), {len(skipped)} skipped")
    if items: print(pd.DataFrame(items).to_string(index=False), flush=True)
    for sd, why in skipped: log(f"skipped {sd['strategy']} {sd['stock']} {sd['direction']} {sd['ref']}: {why}")

    mine = list_my_alerts(k)
    stale = [a["uuid"] for a in mine if a.get("status") == "enabled"]
    if not DRY_RUN:
        for i in range(0, len(stale), 50): k.call("DELETE", "/alerts", tries=2, params=[("uuid", u) for u in stale[i:i + 50]])
    log(f"removed {len(stale)} old SHP| alerts" + (" (dry run: not removed)" if DRY_RUN else ""))

    made, fail = [], 0
    for it in items:
        name = f"{PFX}{it['strategy']}|{it['stock']}|{it['ref']:.2f}"                                   # SHP|WRL|RELIANCE|1234.50
        data = {"name": name, "type": "ato", "lhs_exchange": "NSE", "lhs_tradingsymbol": it["stock"], "lhs_attribute": "LastTradedPrice",
                "operator": ">=" if it["direction"] == "LONG" else "<=", "rhs_type": "constant", "rhs_constant": it["ref"],
                "basket": json.dumps(basket(it["option"], it["qty"], name, it["strategy"]))}
        it = dict(it, alert=name, alert_data=data)                                                     # the manager re-creates the alert from this when the funds allow it again
        if DRY_RUN: log(f"DRY  {name} -> BUY {it['qty']} {it['option']}"); made.append(it); continue
        ok, d = k.call("POST", "/alerts", tries=2, data=data)
        log(("+ " if ok else "! ") + name + f" -> BUY {it['qty']} {it['option']}" + ("" if ok else f"  {d}"))
        if ok: made.append(it)
        else: fail += 1
        time.sleep(0.2)

    plan = dict(created=str(now_ist()), trade_day=str(td), funds=funds, max_cost=cap, product=PRODUCT, strategies={c: STRATS[c] for c in ACTIVE}, items=made)
    if DRY_RUN:                               # a dry run must never replace the real plan the manager will read
        json.dump(plan, open(f"{PLAN_DIR}/preview.json", "w"), indent=1)
        json.dump(plan, open(f"{PLAN_DIR}/preview_{td}.json", "w"), indent=1)
        log(f"DRY RUN: preview saved ({len(made)} orders for {td}) - no alerts created, plan.json untouched")
    else:
        json.dump(plan, open(f"{PLAN_DIR}/plan.json", "w"), indent=1)
        json.dump(plan, open(f"{PLAN_DIR}/plan_{td}.json", "w"), indent=1)
        log(f"plan saved: {len(made)} alerts for {td}, failed={fail}")
    summary(td, funds, cap, made, skipped)
    sys.exit(1 if fail else 0)


def summary(td, funds, cap, made, skipped):
    """A short table on the GitHub run page (Summary tab)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path: return
    L = [f"### SHAPELIVE plan for {td}", f"funds Rs {funds:,.0f} -> Rs {cap:,.0f} per trade" if funds else f"Rs {cap:,.0f} per trade", "",
         "| strategy | stock | option | lots | qty | price | cost |", "|---|---|---|---|---|---|---|"]
    L += [f"| {i['strategy']} | {i['stock']} | {i['option']} | {i['lots']} | {i['qty']} | {i['close_price']} | {i['cost']:,.0f} |" for i in made]
    L += ["", f"{len(made)} orders, {len(skipped)} skipped." + (" Dry run - no alerts created." if DRY_RUN else "")]
    try: open(path, "a").write("\n".join(L) + "\n")
    except Exception: pass


def preview():
    """EVENING PREVIEW (run it after the market closes, e.g. 18:00): which setups, entry levels and options the NEXT trading day will have.  Nothing is created or deleted,
    no plan.json is touched.  Uses the finished daily candles up to today, the closing option prices (Angel One) and the Kite funds for the lot sizes.
    Output: the log, the GitHub run summary and live_plan/preview_next_<day>.json (uploaded as an artifact by the workflow).
    The BPB 'untouched since the open' check can only be made on the day itself, at ALERT_AT; the preview lists every BPB setup before that check."""
    n = now_ist(); today = n.date()
    td = next_trade_day(today) if (n.time() >= dtime(15, 30) or today.weekday() >= 5) else today
    if n.time() < dtime(16, 0) and n.time() >= dtime(15, 30): log("WARNING: before 16:00 today's last daily candle may still be missing - run the preview later")
    log(f"PREVIEW for trade day {td} ({td:%A}); strategies {', '.join(ACTIVE)}.  Exchange holidays are not known here - on a holiday nothing is traded and the daily run ends itself.")
    kite, key, tok = login(); k = K(key, tok)
    os.makedirs(PLAN_DIR, exist_ok=True)
    ang = Angel(); eq, opt = angel_master()
    inst = kite_instruments(kite)
    setups = find_setups(ang, eq)
    picks, skipped = pick_options(setups, inst, td)
    px = option_prices(ang, opt, picks)
    funds = TOTAL_FUNDS if TOTAL_FUNDS > 0 else None
    if funds is None and MAX_COST_FIXED <= 0:
        try: funds = funds_available(k)
        except SystemExit: funds = None
    rows = []
    for sd, i in picks:
        r = STRATS[sd["strategy"]]; p = px.get(i["tradingsymbol"]); lot = int(i["lot_size"])
        cap = MAX_COST_FIXED if MAX_COST_FIXED > 0 else (round(funds * float(r.get("cap_pct") or CAPITAL_PCT) / 100, 2) if funds else 0)
        why, lots = "", 0
        if not p: why = "no price"
        elif p <= MIN_PRICE: why = f"price Rs {p:.2f} is at or below the Rs {MIN_PRICE:g} limit"
        elif cap <= 0: why = "funds unknown - lots not calculated"
        else:
            lots = int(cap // (p * lot))
            if lots < 1: why = f"1 lot costs Rs {p * lot:,.0f} > cap Rs {cap:,.0f}"
        rows.append(dict(strategy=sd["strategy"], stock=sd["stock"], direction=sd["direction"], entry_level=sd["ref"], line=sd["line"], option=i["tradingsymbol"], strike=i["strike"], expiry=str(i["expiry"]),
                         lot_size=lot, price=p, lots=lots, qty=lots * lot, cost=round(lots * lot * (p or 0), 2), tradable=(not why), note=why or ("BPB: checked again at " + ALERT_AT if r.get("check_untouched") else "")))
    for sd, why in skipped: rows.append(dict(strategy=sd["strategy"], stock=sd["stock"], direction=sd["direction"], entry_level=sd["ref"], line=sd["line"], tradable=False, note=why))
    df = pd.DataFrame(rows)
    ok = int(df.tradable.sum()) if len(df) else 0
    log(f"preview {td}: {len(setups)} setups, {ok} tradable orders, {len(df) - ok} not tradable")
    if len(df): print(df.to_string(index=False), flush=True)
    json.dump(dict(created=str(now_ist()), trade_day=str(td), funds=funds, strategies={c: STRATS[c] for c in ACTIVE}, items=rows), open(f"{PLAN_DIR}/preview_next_{td}.json", "w"), indent=1, default=str)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        L = [f"### Preview for {td} ({td:%A}) - {', '.join(STRATS[c]['name'] for c in ACTIVE)}", f"{len(setups)} setups, {ok} tradable (funds {('Rs %s' % format(funds, ',.0f')) if funds else 'unknown'})", "",
             "| strategy | stock | entry level | option | price | lots | qty | cost | note |", "|---|---|---|---|---|---|---|---|---|"]
        for r_ in rows: L.append("| {strategy} | {stock} | {entry_level} | {option} | {price} | {lots} | {qty} | {cost} | {note} |".format(**{k_: r_.get(k_, "") for k_ in ("strategy", "stock", "entry_level", "option", "price", "lots", "qty", "cost", "note")}))
        try: open(path, "a").write("\n".join(L) + "\n")
        except Exception: pass


def probe():
    """Prints your existing ATO alerts exactly as Kite stores them - create one ATO by hand in the Kite app
    (with 10% stop-loss, validity Day) and run this once to see the exact basket / stop-loss layout."""
    kite, key, tok = login(); k = K(key, tok)
    ok, rows = k.call("GET", "/alerts", tries=3, params={"page_size": 100})
    for a in (rows or []):
        if a.get("type") == "ato": print(json.dumps(a, indent=1, default=str), flush=True)
    log("probe done" if ok else f"probe failed: {rows}")


if __name__ == "__main__":
    (probe if (len(sys.argv) > 1 and sys.argv[1] == "probe") else preview if (len(sys.argv) > 1 and sys.argv[1] == "preview") else main)()
