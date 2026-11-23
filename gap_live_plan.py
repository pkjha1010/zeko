"""
GAPLIVE PLAN  -  GAP LONG + GAP SHORT (gap-fade reversal, buy ATM option).  Self-contained: needs no other file of the old zeko code.

THE SETUP (daily candles, same rules as gap_rules.py / gap_backtest.py of the Dhan repo; it is known the evening BEFORE, so the alerts are made before the entry window)
  LONG  : day i opens more than 1 % ABOVE the previous close AND day i+1 closes BELOW day i's close.  Entry level = HIGH of day i.   Entry day = the first day among i+2 .. i+4 whose High reaches the level.
  SHORT : day i opens more than 1 % BELOW the previous close AND day i+1 closes ABOVE day i's close.  Entry level = LOW of day i.    Entry day = the first day among i+2 .. i+4 whose Low reaches the level.
  A setup is used up on the first touch.  This plan lists the setups whose entry window includes TODAY and whose level was not touched on an earlier entry day.

THE TWO STRATEGIES (backtest: Dhan repo, gap_results, premium above Rs 5, exit 15:30)
  GAPL  GAP_LONG   buy the ATM CALL when the stock trades UP through the level, ONLY 09:15:00 - 09:29:59.  SL 60 %, TP 40 % of the premium (built-in GTT), square off 15:30:00.
                   Alerts are created BEFORE the open (run this ~08:45).  At most 4 trades open at the same time.
  GAPS  GAP_SHORT  buy the ATM PUT when the stock trades DOWN through the level, ONLY 09:30:00 - 13:59:59.  SL 60 %, TP 40 %, square off 15:30:00.
                   Alerts are created at ALERT_AT = 09:30:00 and ONLY for setups whose level was NOT touched since the open (a touch before 09:30 would have been a 09:15-09:29 entry = not this strategy).
                   At most 3 trades open at the same time; a 4th is allowed when at least 2 of the 3 open shorts are in profit or within -5 % (the manager checks it live).
  7 % of the Kite funds per trade (cap_pct).  The manager also keeps the account within 30 % of the funds used at a time and can stop new trades after a big day loss.

WHAT IT DOES
  1. Daily candles of the F&O stocks from ANGEL ONE (completed days up to yesterday) -> GAP setups for today.
  2. Picks the option, OUT OF THE MONEY: calls = the first strike at or above the entry level, puts = the first strike strictly below it; nearest MONTHLY expiry, NEXT month once the expiry is ROLL_DAYS (5) trading days away or less.
     Fallback strikes if the first fails the liquidity test: further OTM.  Price from Angel One.
  3. GAPS only: waits until ALERT_AT and DROPS every setup whose entry level was already touched since 09:15 (day low / last price from Angel One).
  4. lots = cap // (price x lot size).  Option price at or below LOW_MIN_PRICE (Rs 1) is skipped; liquidity filter (open interest / spread / depth) on top; real margin checked (MARGIN_CAP_PCT).
  5. One Kite ATO alert per setup: when the STOCK price crosses the entry level (>= for LONG, <= for SHORT) Zerodha sends BUY MARKET for the option (NRML, DAY) with the built-in GTT (+40 % / -60 %).
     Alert names: SHP|GAPL|STOCK|level, SHP|GAPS|STOCK|level.  Orders carry the tag SHPGAPL / SHPGAPS.
  6. Saves live_plan/plan.json (merged with the other strategy of the day when PLAN_MERGE=1; written atomically because the manager reads it while it runs).
Only alerts whose name starts with "SHP|" are touched.  Your other alerts (SWG|...) are not.  A plan step of ONE strategy only replaces the alerts of THAT strategy.
Modes:  python gap_live_plan.py plan     (default)   python gap_live_plan.py preview  (evening: next trading day, creates nothing)   python gap_live_plan.py probe (prints your ATO alerts as Kite stores them)
"""
import os, io, sys, json, time, requests, pyotp, numpy as np, pandas as pd
from datetime import datetime, date, timedelta, time as dtime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect

PFX = "SHP|"
STRATS = {   # code: rules.  entry_end = the alert is deleted / waiting BUYs are cancelled from this second on; exit_at = square-off second.
    # max_open = positions open at the same time (the manager enforces it).  max_open_extra / extra_need / extra_min_pct: the manager allows max_open_extra trades while at least extra_need of the open ones are in profit or
    # not worse than extra_min_pct % of their entry premium.
    "GAPL": dict(name="GAP_LONG", direction="LONG", kind="GAP", entry_end="09:30:00", exit_at="15:30:00", sl_pct=60.0, tp_pct=40.0, max_open=4, cap_pct=7.0, check_untouched=False, chase_pct=5.0, chase_wait_s=0.5),
    "GAPS": dict(name="GAP_SHORT", direction="SHORT", kind="GAP", entry_end="14:00:00", exit_at="15:30:00", sl_pct=60.0, tp_pct=40.0, max_open=3, max_open_extra=4, extra_need=2, extra_min_pct=-5.0, cap_pct=7.0,
                 check_untouched=True, chase_pct=5.0, chase_wait_s=0.5),
}
for _c, _r in STRATS.items():                                   # env overrides, e.g. WRU_TP=60  WRU_SL=20  BPB_EXIT_AT=15:30:00  WRL_ENTRY_END=10:00:00
    for _k, _t in (("tp_pct", "TP"), ("sl_pct", "SL")):
        if os.environ.get(f"{_c}_{_t}"): _r[_k] = float(os.environ[f"{_c}_{_t}"])
    if os.environ.get(f"{_c}_MAX_OPEN"): _r["max_open"] = int(os.environ[f"{_c}_MAX_OPEN"])
    if os.environ.get(f"{_c}_MAX_OPEN_EXTRA") and "max_open_extra" in _r: _r["max_open_extra"] = int(os.environ[f"{_c}_MAX_OPEN_EXTRA"])
    if os.environ.get(f"{_c}_EXTRA_MIN_PCT") and "extra_min_pct" in _r: _r["extra_min_pct"] = float(os.environ[f"{_c}_EXTRA_MIN_PCT"])
    if os.environ.get(f"{_c}_CAPITAL_PCT"): _r["cap_pct"] = float(os.environ[f"{_c}_CAPITAL_PCT"])         # e.g. WRU_CAPITAL_PCT=7  BPB_CAPITAL_PCT=15
    for _k in ("entry_end", "exit_at"):
        if os.environ.get(f"{_c}_{_k.upper()}"): _r[_k] = os.environ[f"{_c}_{_k.upper()}"]
ORDER = ["GAPL", "GAPS"]                                        # priority when two strategies pick the same option
GAP_MIN = float(os.environ.get("GAP_MIN") or 0.01)              # gap size: the open must be more than 1 % away from the previous close (same as the backtest)
GAP_FIRST = int(os.environ.get("GAP_FIRST") or 2)               # entry days are i+2 .. i+4 (same as the backtest)
GAP_LAST = int(os.environ.get("GAP_LAST") or 4)
PLAN_MERGE = (os.environ.get("PLAN_MERGE") or "0") == "1"       # 1 = keep the items / rules of the OTHER strategy already in today's plan.json (GAPL is planned at ~08:45, GAPS at 09:30 into the same plan)
ACTIVE = [c for c in ORDER if c in {x.strip().upper() for x in (os.environ.get("STRATEGIES") or "GAPL,GAPS").replace(" ", "").split(",") if x.strip()}]
CAPITAL_PCT = float(os.environ.get("CAPITAL_PCT") or 7)        # fallback % per trade for a strategy without its own cap_pct (7 % for both WRU and BPB - see STRATS)
MIN_PRICE = float(os.environ.get("MIN_PRICE") or 5)            # strict-test tier: options priced AT OR BELOW this get the strict spread test + LIQ_LOW_MIN_OI_LOTS.  Default 5: options of Rs 1-5 must pass the strict test (spread within 2 ticks, OI at least 1000 lots), dearer ones the normal OI / spread / depth test
# --- LIQUIDITY FILTER (all strategies).  Needs Kite quote data (/quote: depth, OI, volume).  LIQ_FILTER=0 = off = the old rule (price above MIN_PRICE only).
# ON: an option at or below LOW_MIN_PRICE is never traded; between LOW_MIN_PRICE and MIN_PRICE it is traded ONLY if it passes the strict test (tight spread in ticks, deep ask, good OI);
# above MIN_PRICE it must pass the normal test.  Before 09:15  there is no live depth: only the OI test is made.
LIQ_FILTER = (os.environ.get("LIQ_FILTER") or "1") == "1"
LOW_MIN_PRICE = float(os.environ.get("LOW_MIN_PRICE") or 1)             # hard floor (Rs): at or below this never traded.  (The GAP backtests only counted premiums above Rs 5; OTM strikes are cheaper, so the floor is Rs 1 and options up to MIN_PRICE get the STRICT liquidity test.)
FALLBACK_MIN_PRICE = float(os.environ.get("FALLBACK_MIN_PRICE") or 5)   # used ONLY when the liquidity filter is off / the quotes cannot be read: options at or below this are skipped (the old rule)
LIQ_MAX_SPREAD_TICKS = int(os.environ.get("LIQ_MAX_SPREAD_TICKS") or 2) # normal option: ask - bid up to this many ticks (Rs 0.05) ...
LIQ_MAX_SPREAD_PCT = float(os.environ.get("LIQ_MAX_SPREAD_PCT") or 4)   # ... OR up to this % of the mid price
LIQ_LOW_MAX_TICKS = int(os.environ.get("LIQ_LOW_MAX_TICKS") or 2)       # option at or below MIN_PRICE: spread must be within this many ticks (no % alternative)
LIQ_DEPTH_MULT = float(os.environ.get("LIQ_DEPTH_MULT") or 1)           # the 5 best ask levels together must hold at least this x the order quantity (0 = off)
LIQ_OI_X_QTY = float(os.environ.get("LIQ_OI_X_QTY") or 10)              # OPEN INTEREST filter: the option's OI (units) must be at least this many TIMES your order quantity (0 = off).  Adapts to every stock / lot size.
LIQ_ITM_STEPS = int(os.environ.get("LIQ_ITM_STEPS") or 2)               # the first-choice strike fails the test -> try up to this many strikes deeper ITM (puts: higher strike, calls: lower strike), first one that passes is used
ROLL_DAYS = int(os.environ.get("ROLL_DAYS") or 5)                       # stock options: when the monthly expiry is this many trading days away (or less) the NEXT month's contract is used (Zerodha charges the physical-delivery margin on ITM long options from expiry-4 days: up to 25-50 % of the contract value)
MARGIN_CHECK = (os.environ.get("MARGIN_CHECK") or "1") == "1"           # at alert time ask Kite for the real margin of every planned BUY and drop the ones that block too much
MARGIN_CAP_PCT = float(os.environ.get("MARGIN_CAP_PCT") or 15)         # one trade may block at most this % of the funds as margin (premium + any delivery margin)
LIQ_MIN_OI_LOTS = float(os.environ.get("LIQ_MIN_OI_LOTS") or 0)         # optional extra OI filter in LOTS of the contract (Kite gives OI in units; units / lot size = lots).  0 = off (default).
LIQ_LOW_MIN_OI_LOTS = float(os.environ.get("LIQ_LOW_MIN_OI_LOTS") or 1000)   # same, for an option at or below MIN_PRICE (only used when MIN_PRICE > 0)
LIQ_MIN_VOL = float(os.environ.get("LIQ_MIN_VOL") or 0)                 # minimum volume traded today in UNITS, checked only while the market is open (0 = off)
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
ALERT_AT = os.environ.get("ALERT_AT") or "09:30:00"           # GAPS: the alerts are created at this second (after the untouched-since-open check)


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


def save_json(path, obj):
    """Write atomically (the manager reads plan.json while a later plan step may rewrite it)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


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


# ---------------------------------------------------------------- setup logic: GAP LONG / SHORT (same rules as gap_setups() of gap_rules.py)
def gap_armed(O, H, L, C, long, gap=GAP_MIN, first=GAP_FIRST, last=GAP_LAST):
    """Every GAP setup whose entry window includes the NEXT trading day (index n = len(C), not in the data yet) and that has NOT been used up on an earlier entry day.
    LONG : day i has (O[i]-C[i-1])/C[i-1] > gap and C[i+1] < C[i];  level = H[i];  entry days = i+first .. i+last;  used up when an earlier entry day j had H[j] >= level.
    SHORT: day i has (O[i]-C[i-1])/C[i-1] < -gap and C[i+1] > C[i]; level = L[i];  used up when L[j] <= level.
    -> [dict(signal=i, level, day=n-i)]  (day = which entry day today would be: 2, 3 or 4)."""
    O, H, L, C = (np.asarray(x, float) for x in (O, H, L, C))
    n = len(C); out = []
    for i in range(max(1, n - last), n - 1):                           # day i+1 must be a finished candle (i <= n-2); today is n, so i >= n-last
        if i + first > n: continue
        g = (O[i] - C[i - 1]) / C[i - 1]
        if long:
            if not (g > gap and C[i + 1] < C[i]): continue
            lvl = H[i]
            if any(H[j] >= lvl for j in range(i + first, n)): continue
        else:
            if not (g < -gap and C[i + 1] > C[i]): continue
            lvl = L[i]
            if any(L[j] <= lvl for j in range(i + first, n)): continue
        out.append(dict(signal=i, level=float(lvl), day=n - i))
    return out


def gap_pick(arm, long):
    """One setup per stock: when two are armed, the level that price reaches FIRST (LONG: the lowest level, SHORT: the highest)."""
    if not arm: return None
    return min(arm, key=lambda x: x["level"]) if long else max(arm, key=lambda x: x["level"])


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
        O, H, L, C = (df[k].values for k in ("Open", "High", "Low", "Close"))
        for code in ACTIVE:
            r = STRATS[code]; long = r["direction"] == "LONG"
            x = gap_pick(gap_armed(O, H, L, C, long), long)
            if x: out.append(dict(stock=s, direction=r["direction"], ref=round(x["level"], 2), line=round(float(C[x["signal"]]), 2), strategy=code, last=str(df.index[-1].date()),
                                  signal_day=str(df.index[x["signal"]].date()), entry_day=x["day"]))
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


def load_ban():
    """Stocks to skip (F&O ban): symbols in ban_list.txt (repo root; one per line or comma separated, # starts a comment) + the optional BAN_LIST env.  Updated by hand."""
    txt = os.environ.get("BAN_LIST") or ""
    try: txt += "\n" + open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ban_list.txt")).read()
    except OSError: pass
    out = set()
    for line in txt.replace(",", " ").splitlines():
        out |= {w.strip().upper() for w in line.split("#")[0].split() if w.strip()}
    return out


def bdays_to(d0, d1):
    """Weekdays after d0 up to and including d1 (exchange holidays are not known here: ROLL_DAYS has a one-day buffer for that)."""
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5: n += 1
    return n


def pick_options(setups, instruments, trade_day):
    byname = {}
    for i in instruments:
        if i.get("segment") == "NFO-OPT" and i.get("instrument_type") in ("CE", "PE"): byname.setdefault(i["name"], []).append(i)
    picks, skipped = [], []
    for sd in setups:
        lst = byname.get(sd["stock"])
        if not lst: skipped.append((sd, "no option contracts")); continue
        mexp = [e for e in monthly_expiries([i["expiry"] for i in lst]) if e > trade_day and bdays_to(trade_day, e) > ROLL_DAYS]      # inside the last ROLL_DAYS trading days before expiry (incl. expiry day) the NEXT month is used - no delivery margin
        if not mexp: skipped.append((sd, "no monthly expiry left")); continue
        typ = "CE" if sd["direction"] == "LONG" else "PE"
        cands = [i for i in lst if i["expiry"] == mexp[0] and i["instrument_type"] == typ]
        if not cands: skipped.append((sd, "no strikes")); continue
        if sd["direction"] == "LONG":                                  # CALL: the first strike at or above the entry level = OTM (never ITM)
            up = [i for i in cands if float(i["strike"]) >= sd["ref"] - 1e-9]
            if not up: skipped.append((sd, f"no strike at or above the entry level {sd['ref']}")); continue
            best = min(up, key=lambda i: float(i["strike"]))
            deeper = sorted([c for c in cands if float(c["strike"]) > float(best["strike"])], key=lambda c: float(c["strike"]))      # fallbacks: further OTM = higher strikes
        else:                                                          # PUT: the first strike STRICTLY BELOW the entry level = OTM (never at / above it)
            dn = [i for i in cands if float(i["strike"]) < sd["ref"] - 1e-9]
            if not dn: skipped.append((sd, f"no strike below the entry level {sd['ref']}")); continue
            best = max(dn, key=lambda i: float(i["strike"]))
            deeper = sorted([c for c in cands if float(c["strike"]) < float(best["strike"])], key=lambda c: -float(c["strike"]))     # fallbacks: further OTM = lower strikes
        sd["alts"] = deeper[:max(0, LIQ_ITM_STEPS)]                    # fallback strikes (FURTHER OTM, never ITM), used only if the first choice fails the liquidity test
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


def liq_quotes(k, syms):
    """{tradingsymbol: Kite quote dict (depth, oi, volume, last_price)} for NFO options, 200 per request, 1 request/second (Kite limit).  None when the quotes cannot be read (no data plan etc.)."""
    out, syms = {}, sorted(set(syms))
    for i in range(0, len(syms), 200):
        ok, d = k.call("GET", "/quote", tries=2, params=[("i", f"NFO:{s}") for s in syms[i:i + 200]])
        if not ok or not isinstance(d, dict): log(f"liquidity: quotes could not be read ({d})"); return None
        for key, q in d.items(): out[key.split(":", 1)[-1]] = q or {}
        time.sleep(1.1)
    return out


def liquidity(q, qty, cheap, live, lot=1):
    """Liquidity test of one option from its Kite quote.  1st the OPEN INTEREST (in lots of `lot` units), then - only while the market is open - spread and ask depth.
    cheap = priced at or below MIN_PRICE (strict test; off by default).  live = the market is open (depth exists).  -> (ok, text)."""
    oi = float(q.get("oi") or 0); vol = float(q.get("volume") or q.get("volume_traded") or 0)
    dp = q.get("depth") or {}
    buys, sells = dp.get("buy") or [], dp.get("sell") or []
    bid = float((buys[0] or {}).get("price") or 0) if buys else 0.0
    ask = float((sells[0] or {}).get("price") or 0) if sells else 0.0
    min_lots = LIQ_LOW_MIN_OI_LOTS if cheap else LIQ_MIN_OI_LOTS
    oi_lots = oi / lot if lot > 0 else oi
    if min_lots and oi_lots < min_lots: return False, f"open interest {oi_lots:,.0f} lots ({oi:,.0f} units) is below {min_lots:,.0f} lots"
    if LIQ_OI_X_QTY and oi < LIQ_OI_X_QTY * qty: return False, f"open interest {oi:,.0f} units is below {LIQ_OI_X_QTY:g} x the order quantity {qty:,} (= {LIQ_OI_X_QTY * qty:,.0f})"
    if not live: return True, f"OI {oi_lots:,.0f} lots ok (market not open: no depth check)"
    if bid <= 0 or ask <= 0: return False, "no bid or no ask in the market depth"
    spread = ask - bid; ticks = int(round(spread / 0.05)); pct = spread / ((ask + bid) / 2) * 100
    if cheap:
        if ticks > LIQ_LOW_MAX_TICKS: return False, f"bid {bid} ask {ask}: spread {ticks} ticks is above {LIQ_LOW_MAX_TICKS} (cheap option)"
    elif ticks > LIQ_MAX_SPREAD_TICKS and pct > LIQ_MAX_SPREAD_PCT: return False, f"bid {bid} ask {ask}: spread {ticks} ticks = {pct:.1f} % is above {LIQ_MAX_SPREAD_TICKS} ticks / {LIQ_MAX_SPREAD_PCT:g} %"
    ask_qty = sum(int((l or {}).get("quantity") or 0) for l in sells)
    if LIQ_DEPTH_MULT > 0 and ask_qty < LIQ_DEPTH_MULT * qty: return False, f"ask depth {ask_qty:,} is below {LIQ_DEPTH_MULT:g} x the order quantity {qty:,}"
    if LIQ_MIN_VOL and vol < LIQ_MIN_VOL: return False, f"volume today {vol:,.0f} is below {LIQ_MIN_VOL:,.0f}"
    return True, f"bid {bid} ask {ask} ({ticks} ticks, {pct:.1f} %), ask depth {ask_qty:,}, OI {oi_lots:,.0f} lots, volume {vol:,.0f}"


def order_margins(k, items):
    """Real margin Kite will block for each planned BUY (market, NRML) - premium + any physical-delivery margin.  None when the API call fails."""
    body = [dict(exchange="NFO", tradingsymbol=it["option"], transaction_type="BUY", variety="regular", product=PRODUCT, order_type="MARKET", quantity=it["qty"], price=0) for it in items]
    ok, d = k.call("POST", "/margins/orders", tries=2, json=body)
    if not ok or not isinstance(d, list) or len(d) != len(items): log(f"margins/orders failed: {d}"); return None
    return [float(x.get("total") or 0) for x in d]


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
    if t >= dtime(15, 0): sys.exit(f"It is {t:%H:%M}: too late to plan today's entries (they stop at 14:00). Nothing planned.")
    return today


def sleep_until(hms):
    h, m, *s = [int(x) for x in hms.split(":")]
    target = dtime(h, m, s[0] if s else 0)
    while now_ist().time() < target:
        time.sleep(min(0.5, max(0.01, (datetime.combine(now_ist().date(), target) - now_ist()).total_seconds())))


def quote_days(q):
    """Dates found in a Kite quote ('last_trade_time' and the exchange 'timestamp').  An index quote may carry only one of them, or a zero date - those are ignored."""
    out = set()
    for key in ("timestamp", "last_trade_time"):
        try:
            dt = datetime.strptime(str((q or {}).get(key) or "")[:19], "%Y-%m-%d %H:%M:%S")
            if dt.year > 2000: out.add(dt.date())
        except Exception:
            pass
    return out


def market_open_today(k):
    """True unless the NIFTY 50 quote CLEARLY shows no trade today.  Unknown (call failed, no usable date, before the open) = open: a wrong 'holiday' leaves the day unmanaged, a wrong 'open' costs nothing."""
    if now_ist().time() < dtime(9, 16): return True                      # no NIFTY trade before the open: the check cannot work yet
    ok, d = k.call("GET", "/quote", tries=3, params=[("i", "NSE:NIFTY 50")])
    if not ok or not d: return True
    q = d.get("NSE:NIFTY 50") or {}
    days = quote_days(q)
    log(f"holiday check: NIFTY quote timestamp={q.get('timestamp')!r} last_trade_time={q.get('last_trade_time')!r} last_price={q.get('last_price')!r}")
    if not days: return True
    return now_ist().date() in days


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
    log(f"planning trade day {td}" + ("  [dry run: no alerts created, plan.json untouched]" if DRY_RUN else "  (strategies with the untouched-since-open check get their alerts at " + ALERT_AT + ", the others at once; plan.json is saved)"))
    kite, key, tok = login(); k = K(key, tok)
    os.makedirs(PLAN_DIR, exist_ok=True)
    if not FORCE and not market_open_today(k):
        log("No trading today (NIFTY has no trade yet) - empty plan, nothing created")
        save_json(f"{PLAN_DIR}/{'preview' if DRY_RUN else 'plan'}.json", merge_plan(dict(created=str(now_ist()), trade_day=str(td), funds=None, max_cost=0, product=PRODUCT, strategies={c: STRATS[c] for c in ACTIVE}, items=[]), td) if (PLAN_MERGE and not DRY_RUN) else dict(created=str(now_ist()), trade_day=str(td), funds=None, max_cost=0, product=PRODUCT, strategies={c: STRATS[c] for c in ACTIVE}, items=[]))
        sys.exit(0)
    ang = Angel(); eq, opt = angel_master()
    inst = kite_instruments(kite)                                      # fetched FIRST: if Kite is slow we know in seconds, not after the 10+ minute candle scan
    setups = find_setups(ang, eq)
    picks, skipped = pick_options(setups, inst, td)
    ban = load_ban()
    if ban:
        for sd, i in picks:
            if sd["stock"].upper() in ban: skipped.append((sd, "on the F&O ban list (ban_list.txt)"))
        picks = [(sd, i) for sd, i in picks if sd["stock"].upper() not in ban]
        log(f"ban list: {len(ban)} stocks ({', '.join(sorted(ban))[:120]})")
    if not FORCE:                                                      # a strategy whose entry time is already over today gets no alerts (they would fire into the late-entry sell-off)
        t_now = now_ist().time(); over = {id(sd) for sd, i in picks if t_now >= dtime(*[int(x) for x in STRATS[sd["strategy"]]["entry_end"].split(":")])}
        for sd, i in picks:
            if id(sd) in over: skipped.append((sd, f"entry time over ({STRATS[sd['strategy']]['entry_end']}) - not planned"))
        picks = [(sd, i) for sd, i in picks if id(sd) not in over]
    px = option_prices(ang, opt, picks + [(sd, a) for sd, i in picks for a in (sd.get("alts") or [])])      # everything slow is done BEFORE the alerts are created (also the ITM fallback strikes)
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
    lq = liq_quotes(k, [c["tradingsymbol"] for sd, i in picks for c in [i] + (sd.get("alts") or [])]) if (LIQ_FILTER and picks) else None      # live depth / OI of the chosen options + their ITM fallbacks (read as late as possible)
    use_liq = lq is not None
    if LIQ_FILTER and not use_liq: log(f"!!! liquidity filter could not run - falling back to the plain price rule (price above Rs {FALLBACK_MIN_PRICE:g})")
    live = now_ist().time() >= dtime(9, 15)                            # before the open there is no depth
    for sd, i0 in picks:                                               # picks are in strategy priority order (WRU, WRL, BPB)
        if i0["tradingsymbol"] in taken: skipped.append((sd, f"{i0['tradingsymbol']}: already planned by {taken[i0['tradingsymbol']]} (one option = one strategy)")); continue
        scap = caps[sd["strategy"]]
        floor = LOW_MIN_PRICE if use_liq else FALLBACK_MIN_PRICE       # liquidity filter on: only the Rs 1 hard floor here, the OI / spread / depth test follows below
        chosen, tried = None, []
        for step, i in enumerate([i0] + ((sd.get("alts") or []) if use_liq else [])):      # first choice, then up to LIQ_ITM_STEPS deeper ITM strikes: the first one that passes is used
            ts, tag = i["tradingsymbol"], ("first choice" if step == 0 else f"OTM {step}")
            p = px.get(ts); lot = int(i["lot_size"])
            if not p: tried.append(f"{ts} ({tag}): no price"); continue
            if p <= floor: tried.append(f"{ts} ({tag}): price Rs {p:.2f} is at or below the Rs {floor:g} limit"); continue
            lots = int(scap // (p * lot))
            if lots < 1: tried.append(f"{ts} ({tag}): 1 lot costs Rs {p * lot:,.0f} > cap Rs {scap:,.0f}"); continue
            if ts in taken: tried.append(f"{ts} ({tag}): already planned by {taken[ts]}"); continue
            if use_liq:
                good, why = liquidity(lq.get(ts) or {}, lots * lot, p <= MIN_PRICE, live, lot)
                if not good: tried.append(f"{ts} ({tag}): not liquid enough - {why}"); continue
                log(f"liquidity {ts} ({tag}, Rs {p:.2f}): {why}")
            chosen = (i, p, lot, lots); break
        if not chosen: skipped.append((sd, "; ".join(tried) or "no option")); continue
        i, p, lot, lots = chosen
        if tried: log(f"{sd['stock']}: first choice not usable -> using {i['tradingsymbol']} instead (" + "; ".join(tried) + ")")
        taken[i["tradingsymbol"]] = sd["strategy"]
        total += lots * lot * p
        items.append(dict(stock=sd["stock"], direction=sd["direction"], strategy=sd["strategy"], ref=sd["ref"], line=sd["line"], option=i["tradingsymbol"], strike=i["strike"],
                          expiry=str(i["expiry"]), lot_size=lot, lots=lots, qty=lots * lot, close_price=p, cost=round(lots * lot * p, 2)))
    mbase = funds if funds else (funds_available(k) or 0)
    if items and MARGIN_CHECK and mbase > 0:
        ms = order_margins(k, items)
        if ms is None: log("!!! margin check could not run (Kite margins API) - plan kept as it is; the manager's fund limit still protects the account")
        else:
            mlimit, keep = mbase * MARGIN_CAP_PCT / 100, []
            for it, m in zip(items, ms):
                it["margin"] = round(m, 2)
                log(f"margin {it['option']}: Rs {m:,.0f} (premium Rs {it['cost']:,.0f}) vs limit Rs {mlimit:,.0f} ({MARGIN_CAP_PCT:g}% of Rs {mbase:,.0f})")
                if m > mlimit: skipped.append((dict(strategy=it["strategy"], stock=it["stock"], direction=it["direction"], ref=it["ref"]), f"{it['option']}: margin Rs {m:,.0f} is above {MARGIN_CAP_PCT:g}% of the funds (Rs {mlimit:,.0f}) - premium only Rs {it['cost']:,.0f}"))
                else: keep.append(it)
            items = keep; total = sum(it["cost"] for it in items)
    log(f"total cost of all planned orders Rs {total:,.0f}")
    log(f"trade day {td}: {len(setups)} setups ({len(picks)} untouched) -> {len(items)} orders within the per-trade cap ({', '.join(f'{c} Rs {v:,.0f}' for c, v in caps.items())}), {len(skipped)} skipped")
    if items: print(pd.DataFrame(items).to_string(index=False), flush=True)
    for sd, why in skipped: log(f"skipped {sd['strategy']} {sd['stock']} {sd['direction']} {sd['ref']}: {why}")

    mine = list_my_alerts(k)
    stale = [a["uuid"] for a in mine if a.get("status") == "enabled" and any(str(a.get("name", "")).startswith(f"{PFX}{c}|") for c in ACTIVE)]      # ONLY the alerts of the strategies planned now (the other strategy's alerts of today stay)
    if not DRY_RUN:
        for i in range(0, len(stale), 50): k.call("DELETE", "/alerts", tries=2, params=[("uuid", u) for u in stale[i:i + 50]])
    log(f"removed {len(stale)} old SHP| alerts of {', '.join(ACTIVE)}" + (" (dry run: not removed)" if DRY_RUN else ""))

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
        save_json(f"{PLAN_DIR}/preview.json", plan)
        save_json(f"{PLAN_DIR}/preview_{td}.json", plan)
        log(f"DRY RUN: preview saved ({len(made)} orders for {td}) - no alerts created, plan.json untouched")
    else:
        if PLAN_MERGE: plan = merge_plan(plan, td)
        save_json(f"{PLAN_DIR}/plan.json", plan)
        save_json(f"{PLAN_DIR}/plan_{td}.json", plan)
        log(f"plan saved: {len(made)} alerts for {td}, failed={fail}" + (f"  (plan.json now holds {len(plan['items'])} items of {', '.join(plan['strategies'])})" if PLAN_MERGE else ""))
    summary(td, funds, cap, made, skipped)
    sys.exit(1 if fail else 0)


def merge_plan(plan, td):
    """PLAN_MERGE=1: today's plan.json already written by an earlier plan step (WRU + ENG in the morning) keeps its items and rules; this step's strategies replace only their own.
    The manager that starts after this step then manages every strategy of the day."""
    p = f"{PLAN_DIR}/plan.json"
    try: old = json.load(open(p))
    except Exception: return plan
    if str(old.get("trade_day")) != str(td): return plan                     # an old day's file: ignore
    keep = [i for i in old.get("items", []) if i.get("strategy") not in plan["strategies"]]
    rules = {c: r for c, r in (old.get("strategies") or {}).items() if c not in plan["strategies"]}
    rules.update(plan["strategies"])
    log(f"merged with the earlier plan of today: kept {len(keep)} items of {', '.join(sorted({i.get('strategy', '?') for i in keep})) or '-'}")
    return dict(plan, strategies=rules, items=keep + plan["items"])


def summary(td, funds, cap, made, skipped):
    """A short table on the GitHub run page (Summary tab)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path: return
    L = [f"### GAPLIVE plan for {td}", f"funds Rs {funds:,.0f} -> Rs {cap:,.0f} per trade" if funds else f"Rs {cap:,.0f} per trade", "",
         "| strategy | stock | option | lots | qty | price | cost |", "|---|---|---|---|---|---|---|"]
    L += [f"| {i['strategy']} | {i['stock']} | {i['option']} | {i['lots']} | {i['qty']} | {i['close_price']} | {i['cost']:,.0f} |" for i in made]
    L += ["", f"{len(made)} orders, {len(skipped)} skipped." + (" Dry run - no alerts created." if DRY_RUN else "")]
    try: open(path, "a").write("\n".join(L) + "\n")
    except Exception: pass


def preview():
    """EVENING PREVIEW (run it after the market closes, e.g. 18:00): which setups, entry levels and options the NEXT trading day will have.  Nothing is created or deleted,
    no plan.json is touched.  Uses the finished daily candles up to today, the closing option prices (Angel One) and the Kite funds for the lot sizes.
    Output: the log, the GitHub run summary and live_plan/preview_next_<day>.json (uploaded as an artifact by the workflow).
    The GAPS 'untouched since the open' check can only be made on the day itself, at ALERT_AT; the preview lists every setup before that check."""
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
    lq = liq_quotes(k, [i["tradingsymbol"] for sd, i in picks]) if (LIQ_FILTER and picks) else None
    use_liq = lq is not None
    for sd, i in picks:
        r = STRATS[sd["strategy"]]; p = px.get(i["tradingsymbol"]); lot = int(i["lot_size"])
        cap = MAX_COST_FIXED if MAX_COST_FIXED > 0 else (round(funds * float(r.get("cap_pct") or CAPITAL_PCT) / 100, 2) if funds else 0)
        why, lots = "", 0
        floor = LOW_MIN_PRICE if use_liq else FALLBACK_MIN_PRICE
        if not p: why = "no price"
        elif p <= floor: why = f"price Rs {p:.2f} is at or below the Rs {floor:g} limit"
        elif cap <= 0: why = "funds unknown - lots not calculated"
        else:
            lots = int(cap // (p * lot))
            if lots < 1: why = f"1 lot costs Rs {p * lot:,.0f} > cap Rs {cap:,.0f}"
            elif use_liq:                                              # evening preview: no live depth, only the open-interest test (the full test is made at plan time)
                good, lw = liquidity(lq.get(i["tradingsymbol"]) or {}, lots * lot, p <= MIN_PRICE, False, lot)
                if not good: why = f"not liquid enough - {lw}"
        rows.append(dict(strategy=sd["strategy"], stock=sd["stock"], direction=sd["direction"], entry_level=sd["ref"], line=sd["line"], option=i["tradingsymbol"], strike=i["strike"], expiry=str(i["expiry"]),
                         lot_size=lot, price=p, lots=lots, qty=lots * lot, cost=round(lots * lot * (p or 0), 2), tradable=(not why), note=why or (f"{sd['strategy']}: untouched-since-open check at " + ALERT_AT if r.get("check_untouched") else "")))
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
