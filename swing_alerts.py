import os, sys, time, requests, pyotp, pandas as pd, yfinance as yf
from datetime import datetime, time as dtime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect

IST, P, B = ZoneInfo("Asia/Kolkata"), "SWG|", "https://api.kite.trade/alerts"
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


def login():
    key, uid = os.environ["KITE_API_KEY"], os.environ["ZERODHA_USER_ID"]
    s = requests.Session()
    r = s.post("https://kite.zerodha.com/api/login", timeout=30, data={
        "user_id": uid, "password": os.environ["ZERODHA_PASSWORD"]}).json()
    if r.get("status") != "success": sys.exit(f"Login failed: {r}")
    r = s.post("https://kite.zerodha.com/api/twofa", timeout=30, data={
        "user_id": uid, "request_id": r["data"]["request_id"], "twofa_type": "totp",
        "twofa_value": pyotp.TOTP(os.environ["ZERODHA_TOTP_KEY"]).now()}).json()
    if r.get("status") != "success": sys.exit(f"2FA failed: {r}")
    url = f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"
    for _ in range(10):
        loc = s.get(url, allow_redirects=False, timeout=30).headers.get("Location")
        if not loc: break
        url = urljoin(url, loc)
        if "request_token" in url: break
    kite = KiteConnect(api_key=key)
    d = kite.generate_session(parse_qs(urlparse(url).query)["request_token"][0],
                              api_secret=os.environ["KITE_API_SECRET"])
    kite.set_access_token(d["access_token"])
    return kite, {"X-Kite-Version": "3", "Authorization": f"token {key}:{d['access_token']}"}


def candles(s):
    try:
        df = DATA[s + ".NS"][["Open", "High", "Low", "Close"]].dropna()
    except KeyError:
        return None
    now = datetime.now(IST)
    if len(df) and df.index[-1].date() == now.date() and now.time() < dtime(15, 45):
        df = df.iloc[:-1]  # today's candle still forming
    return df


def shape(o, h, l, c):
    """Last candle: body < 45% of range and a wick (upper or lower) >= 2x body."""
    if h == l: return False
    b = abs(c - o)
    return b / (h - l) < .45 and max(h - max(o, c), min(o, c) - l) >= 2 * b


def setup(df, long):
    """Most recent valid break-of-structure setup for the last candle, else None.
    Returns (ref_line, swing_date, bos_date, pullback_date)."""
    n, res = len(df) - 1, None
    hi, lo, c = df.High.values, df.Low.values, df.Close.values
    for i in range(10, n):
        w = (hi if long else lo)[i - 10:i]            # 10 days before candle i
        k = w.argmax() if long else w.argmin()
        ref, sw = w[k], i - 10 + k                    # reference line + swing candle
        if not (c[i] > ref if long else c[i] < ref): continue         # break of structure
        if i - sw > 20 or n - i > 20: continue                        # 20-day limits
        if not (c[n] < ref if long else c[n] > ref): continue         # last candle closes back across line
        # pullback = first close back across the line after the break (can be the last candle)
        pb = next(j for j in range(i + 1, n + 1) if (c[j] < ref if long else c[j] > ref))
        res = (ref, df.index[sw], df.index[i], df.index[pb])          # later break overrides
    return res


kite, H = login()
print("Kite login OK", flush=True)
DATA = yf.download([s + ".NS" for s in UNIVERSE], period="90d", group_by="ticker",
                   auto_adjust=False, progress=False, threads=True)
have_data = sum(1 for s in UNIVERSE if (candles(s) is not None and len(candles(s)) >= 40))
print(f"Prices downloaded: {have_data}/{len(UNIVERSE)} stocks have data", flush=True)
if have_data < 150: sys.exit("Price download failed - alerts left untouched")

want, rows, passed = {}, [], 0
for s in UNIVERSE:
    df = candles(s)
    if df is None or len(df) < 40: continue
    o, h, l, c = df.iloc[-1][["Open", "High", "Low", "Close"]]
    if not shape(o, h, l, c): continue
    passed += 1
    for long in (True, False):
        r = setup(df, long)
        if r is None: continue
        ref, sw, bos, pb = r
        lvl, d = round(float(ref), 2), "LONG" if long else "SHORT"
        name = f"{P}{d}|{s}|{lvl:.2f}"
        rows.append(dict(Stock=s, Direction=d, Ref_Line=lvl, Swing=sw.date(), BoS=bos.date(),
                         Pullback=pb.date(), Last_Candle=df.index[-1].date()))
        want[name] = dict(name=name, type="simple", lhs_exchange="NSE", lhs_tradingsymbol=s,
                          lhs_attribute="LastTradedPrice", operator=">=" if long else "<=",
                          rhs_type="constant", rhs_constant=lvl)
print(f"Last candle test passed: {passed} stocks", flush=True)
print(pd.DataFrame(rows).to_string(index=False) if rows else "No setups today", flush=True)

have, p = {}, 1
while p < 20:  # existing alerts made by this script
    rows = requests.get(B, headers=H, params={"page": p, "page_size": 100}).json()["data"] or []
    have.update({a["uuid"]: a for a in rows})
    if len(rows) < 100: break
    p += 1
have = {a["name"]: a for a in have.values() if a["name"].startswith(P) and a["status"] != "deleted"}
stale = [a["uuid"] for n, a in have.items() if n not in want and a["status"] == "enabled"]
for i in range(0, len(stale), 50):
    requests.delete(B, headers=H, params=[("uuid", u) for u in stale[i:i + 50]])

fail = 0
for n, a in want.items():
    if n in have: continue
    r = requests.post(B, headers=H, data=a)
    print(("+ " if r.ok else "! ") + n, "" if r.ok else r.text[:150])
    fail += not r.ok
    time.sleep(0.15)
print(f"signals={len(want)} deleted_stale={len(stale)} failed={fail}")
sys.exit(1 if fail else 0)
