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
    if h == l: return False
    b, up, lo = abs(c - o), h - max(o, c), min(o, c) - l
    return b / (h - l) <= .45 and ((up >= 2*b and up > 1.5*lo) or (lo >= 2*b and lo > 1.5*up))


def swing_date(df, long):
    """Your Colab logic. Returns swing date of the last valid BoS, or None."""
    n, col = len(df) - 1, "High" if long else "Low"
    f = max if long else min
    ref = df[col].rolling(10).apply(f, raw=True).shift(1)
    loc = df[col].rolling(10).apply(lambda x: x.argmax() if long else x.argmin(), raw=True).shift(1)
    c = df.Close
    if pd.isna(ref.iloc[n]) or (c.iloc[n] >= ref.iloc[n] if long else c.iloc[n] <= ref.iloc[n]):
        return None
    res = None
    for i in range(10, n):
        r = ref.iloc[i]
        if pd.isna(r) or not (c.iloc[i] > r if long else c.iloc[i] < r) or n - i > 25: continue
        if any((df.Low.iloc[j] < r) if long else (df.High.iloc[j] > r) for j in range(i + 1, n)):
            res = df.index[i - 10 + int(loc.iloc[i])]
    return res


kite, H = login()
print("Kite login OK", flush=True)
DATA = yf.download([s + ".NS" for s in UNIVERSE], period="90d", group_by="ticker",
                   auto_adjust=False, progress=False, threads=True)
print("Prices downloaded", flush=True)

want = {}
for s in UNIVERSE:
    df = candles(s)
    if df is None or len(df) < 40: continue
    o, h, l, c = df.iloc[-1][["Open", "High", "Low", "Close"]]
    if not shape(o, h, l, c): continue
    for long in (True, False):
        d = swing_date(df, long)
        if d is None: continue
        lvl = round(round((df.High if long else df.Low)[d] / .05) * .05, 2)
        if (c >= lvl) if long else (c <= lvl):
            print(f"skip {s}: already past {lvl}"); continue
        name = f"{P}{'LONG' if long else 'SHORT'}|{s}|{lvl:.2f}"
        want[name] = dict(name=name, type="simple", lhs_exchange="NSE", lhs_tradingsymbol=s,
                          lhs_attribute="LastTradedPrice", operator=">=" if long else "<=",
                          rhs_type="constant", rhs_constant=lvl)

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
