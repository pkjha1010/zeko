"""
swing_alerts.py
---------------
1. Logs in to Zerodha Kite automatically (same secrets as the ATO cleanup workflow).
2. Gets daily prices from the Kite API and runs the Swing / Break-of-Structure
   scanner (same logic as the Colab notebook) on the F&O universe
   -- or reads a signals CSV with --csv.
3. Entry price per signal (Swing_Date candle, from Kite):
       LONG  -> High of Swing_Date -> alert when LTP >= that price (crosses UP)
       SHORT -> Low  of Swing_Date -> alert when LTP <= that price (crosses DOWN)
4. Creates simple Kite price alerts (notification only, NO orders).

Alerts are named "SWG|<DIR>|<SYMBOL>|<price>" so re-runs are safe:
   * same alert already there         -> left alone (no duplicates)
   * old SWG alert no longer a signal -> deleted (only if still enabled)
   * only alerts whose name starts with "SWG|" are ever touched.

Env vars: KITE_API_KEY, KITE_API_SECRET, ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_KEY

Needs the Kite Connect plan that includes historical data.

Usage:
   python swing_alerts.py                    # scan + create alerts
   python swing_alerts.py --dry-run          # scan + print, NO alerts created
   python swing_alerts.py --csv signals.csv  # skip scan, use Colab CSV
"""
import argparse
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time as dtime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
PREFIX = "SWG|"
MAX_DAYS_SINCE_BOS = 25
LOOKBACK_DAYS = 90      # calendar days, same as yfinance period="90d"
TICK = 0.05

UNIVERSE = [
    "360ONE.NS", "ABCAPITAL.NS", "ADANIENSOL.NS", "ABB.NS", "ADANIPORTS.NS", "AMBER.NS",
    "ASIANPAINT.NS", "AMBUJACEM.NS", "ADANIENT.NS", "BAJAJ-AUTO.NS", "BAJAJHLDNG.NS",
    "APLAPOLLO.NS", "ADANIGREEN.NS", "AUROPHARMA.NS", "AXISBANK.NS", "BOSCHLTD.NS",
    "BRITANNIA.NS", "CDSL.NS", "BANDHANBNK.NS", "BANKBARODA.NS", "BDL.NS",
    "BHARATFORG.NS", "COALINDIA.NS", "BHARTIARTL.NS", "BLUESTARCO.NS", "BPCL.NS",
    "ALKEM.NS", "CAMS.NS", "CHOLAFIN.NS", "CIPLA.NS", "COCHINSHIP.NS", "COLPAL.NS",
    "COFORGE.NS", "CROMPTON.NS", "CONCOR.NS", "DABUR.NS", "ANGELONE.NS",
    "APOLLOHOSP.NS", "DELHIVERY.NS", "DIVISLAB.NS", "DLF.NS", "DMART.NS", "DRREDDY.NS",
    "DIXON.NS", "ETERNAL.NS", "FORCEMOT.NS", "FORTIS.NS", "GLENMARK.NS",
    "GODFRYPHLP.NS", "BANKINDIA.NS", "GODREJPROP.NS", "GODREJCP.NS", "HCLTECH.NS",
    "HDFCBANK.NS", "GRASIM.NS", "GVT&D.NS", "HAVELLS.NS", "HDFCLIFE.NS", "BEL.NS",
    "BHEL.NS", "HINDALCO.NS", "BIOCON.NS", "HINDUNILVR.NS", "HINDZINC.NS",
    "IDFCFIRSTB.NS", "INDUSINDBK.NS", "ICICIGI.NS", "CGPOWER.NS", "IRFC.NS",
    "JINDALSTEL.NS", "JIOFIN.NS", "IEX.NS", "INDHOTEL.NS", "INDIANB.NS", "JSWENERGY.NS",
    "JSWSTEEL.NS", "KAYNES.NS", "KEI.NS", "INFY.NS", "INOXWIND.NS", "ITC.NS",
    "KPITTECH.NS", "JUBLFOOD.NS", "LT.NS", "M&M.NS", "KALYANKJIL.NS", "MANKIND.NS",
    "MARICO.NS", "ADANIPOWER.NS", "MCX.NS", "NAM-INDIA.NS", "KFINTECH.NS",
    "NESTLEIND.NS", "EICHERMOT.NS", "LODHA.NS", "FEDERALBNK.NS", "OBEROIRLTY.NS",
    "LTM.NS", "LUPIN.NS", "PAGEIND.NS", "MANAPPURAM.NS", "PAYTM.NS", "PFC.NS",
    "PGEL.NS", "MARUTI.NS", "MAXHEALTH.NS", "PHOENIXLTD.NS", "PNB.NS", "MOTHERSON.NS",
    "POLYCAB.NS", "POWERINDIA.NS", "NMDC.NS", "OFSS.NS", "PRESTIGE.NS", "OIL.NS",
    "ONGC.NS", "PATANJALI.NS", "RECLTD.NS", "SBILIFE.NS", "GMRAIRPORT.NS", "SBIN.NS",
    "SHREECEM.NS", "TATACONSUM.NS", "PETRONET.NS", "TATAELXSI.NS", "TATASTEEL.NS",
    "TMPV.NS", "UPL.NS", "PIDILITIND.NS", "WAAREEENER.NS", "PIIND.NS", "BAJAJFINSV.NS",
    "POLICYBZR.NS", "PREMIERENE.NS", "RADICO.NS", "RBLBANK.NS", "SAIL.NS",
    "SOLARINDS.NS", "SUNPHARMA.NS", "SUPREMEIND.NS", "SUZLON.NS", "SWIGGY.NS",
    "TATAPOWER.NS", "TCS.NS", "TIINDIA.NS", "TITAN.NS", "TVSMOTOR.NS", "UNIONBANK.NS",
    "VBL.NS", "VEDL.NS", "ZYDUSLIFE.NS", "HEROMOTOCO.NS", "HYUNDAI.NS", "ICICIBANK.NS",
    "INDIGO.NS", "BSE.NS", "LICHSGFIN.NS", "LTF.NS", "HDFCAMC.NS", "MOTILALOFS.NS",
    "MPHASIS.NS", "NATIONALUM.NS", "NBCC.NS", "NYKAA.NS", "ICICIPRULI.NS",
    "PERSISTENT.NS", "PNBHOUSING.NS", "POWERGRID.NS", "LAURUSLABS.NS", "RELIANCE.NS",
    "MAZDOCK.NS", "SONACOMS.NS", "NAUKRI.NS", "SRF.NS", "TECHM.NS", "ULTRACEMCO.NS",
    "UNOMINDA.NS", "VMM.NS", "WIPRO.NS", "ASTRAL.NS", "BAJFINANCE.NS", "UNITDSPR.NS",
    "ASHOKLEY.NS", "MUTHOOTFIN.NS", "CUMMINSIND.NS", "GAIL.NS", "SIEMENS.NS", "MFSL.NS",
    "NHPC.NS", "NTPC.NS", "TORNTPHARM.NS", "RVNL.NS", "KOTAKBANK.NS", "LICI.NS",
    "AUBANK.NS", "HAL.NS", "IDEA.NS", "INDUSTOWER.NS", "IOC.NS", "IREDA.NS", "TRENT.NS",
    "SBICARD.NS", "SHRIRAMFIN.NS", "CANBK.NS", "YESBANK.NS", "VOLTAS.NS",
    "HINDPETRO.NS", "ATHERENERG.NS", "MAHABANK.NS", "SAGILITY.NS",
]


# ----------------------------------------------------------------------
# Kite login (user id + password + TOTP, then request_token -> access_token)
# ----------------------------------------------------------------------
def kite_login():
    import pyotp
    from kiteconnect import KiteConnect

    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id = os.environ["ZERODHA_USER_ID"]
    password = os.environ["ZERODHA_PASSWORD"]
    totp = pyotp.TOTP(os.environ["ZERODHA_TOTP_KEY"]).now()

    s = requests.Session()
    r = s.post("https://kite.zerodha.com/api/login",
               data={"user_id": user_id, "password": password}, timeout=30)
    r.raise_for_status()
    request_id = r.json()["data"]["request_id"]
    r = s.post("https://kite.zerodha.com/api/twofa",
               data={"user_id": user_id, "request_id": request_id,
                     "twofa_value": totp, "twofa_type": "totp"}, timeout=30)
    r.raise_for_status()

    url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
    request_token = None
    for _ in range(10):
        r = s.get(url, allow_redirects=False, timeout=30)
        loc = r.headers.get("Location")
        if not loc:
            break
        url = urljoin(url, loc)
        q = parse_qs(urlparse(url).query)
        if "request_token" in q:
            request_token = q["request_token"][0]
            break
    if not request_token:
        raise RuntimeError("Could not obtain request_token (is the Kite app authorised?)")

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(data["access_token"])
    return kite, api_key, data["access_token"]


# ----------------------------------------------------------------------
# Prices from Kite
# ----------------------------------------------------------------------
KITE = None
TOKENS = {}      # NSE trading symbol -> instrument token
_CACHE = {}      # symbol -> DataFrame
_LOCK = threading.Lock()
_LAST_CALL = [0.0]


def _throttle(gap=0.34):
    """Kite historical API allows 3 requests/sec - space the calls out."""
    with _LOCK:
        wait = _LAST_CALL[0] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()


def init_prices(kite):
    global KITE, TOKENS
    KITE = kite
    TOKENS = {i["tradingsymbol"]: i["instrument_token"]
              for i in kite.instruments("NSE") if i.get("instrument_type") == "EQ"}
    print(f"Loaded {len(TOKENS)} NSE equity instruments.")


def get_df(sym):
    """Daily candles for the last ~90 days as DataFrame (Open/High/Low/Close).
    Today's candle is dropped if the market is still open (incomplete)."""
    if sym in _CACHE:
        return _CACHE[sym]
    df = None
    tok = TOKENS.get(sym)
    if tok is None:
        print(f"  no NSE instrument for {sym}")
    else:
        now = datetime.now(IST)
        for attempt in range(3):
            try:
                _throttle()
                rows = KITE.historical_data(tok, now - timedelta(days=LOOKBACK_DAYS), now, "day")
                break
            except Exception as e:
                rows = None
                time.sleep(1.0 * (attempt + 1))
        if rows:
            df = pd.DataFrame(rows).rename(columns={"open": "Open", "high": "High",
                                                    "low": "Low", "close": "Close"})
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
            df = df.set_index("date")[["Open", "High", "Low", "Close"]]
            if df.index[-1].date() == now.date() and now.time() < dtime(15, 45):
                df = df.iloc[:-1]
        else:
            print(f"  no price data for {sym}")
    _CACHE[sym] = df
    return df


# ----------------------------------------------------------------------
# Scanner  (logic copied 1:1 from the Colab notebook)
# ----------------------------------------------------------------------
def scan_ticker(ticker):
    results = []
    try:
        df = get_df(ticker.replace('.NS', ''))
        if df is None or len(df) < 40:
            return results

        df = df.copy()
        val_idx = len(df) - 1
        p_close = df['Close'].iloc[val_idx]
        p_open = df['Open'].iloc[val_idx]
        p_high = df['High'].iloc[val_idx]
        p_low = df['Low'].iloc[val_idx]

        body_size = abs(p_close - p_open)
        candle_range = p_high - p_low
        if candle_range == 0:
            return results
        body_ratio = body_size / candle_range
        upper_wick = p_high - max(p_open, p_close)
        lower_wick = min(p_open, p_close) - p_low

        wick_cond = (
            (upper_wick >= 2 * body_size and upper_wick > 1.5 * lower_wick) or
            (lower_wick >= 2 * body_size and lower_wick > 1.5 * upper_wick)
        )
        if body_ratio > 0.45 or not wick_cond:
            return results

        def fmt(d):
            return d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)

        # ---- LONG
        df['Ref_Line_High'] = df['High'].rolling(window=10).max().shift(1)
        rolling_max_idx = df['High'].rolling(window=10).apply(lambda x: x.argmax(), raw=True).shift(1)
        ref_val_long = df['Ref_Line_High'].iloc[val_idx]
        if not pd.isna(ref_val_long) and p_close < ref_val_long:
            best = None
            i = 10
            while i < val_idx:
                r_val = df['Ref_Line_High'].iloc[i]
                if not pd.isna(r_val) and df['Close'].iloc[i] > r_val:
                    bos_idx = i
                    if (val_idx - bos_idx) <= MAX_DAYS_SINCE_BOS:
                        for j in range(bos_idx + 1, val_idx):
                            if df['Low'].iloc[j] < r_val:
                                sh_loc = int(rolling_max_idx.iloc[i]) if not pd.isna(rolling_max_idx.iloc[i]) else (i - 10)
                                best = {
                                    'Ticker': ticker.replace('.NS', ''),
                                    'Direction': 'LONG',
                                    'BoS_Date': fmt(df.index[bos_idx]),
                                    'Swing_Date': fmt(df.index[i - 10 + sh_loc]),
                                    'Today_Date': fmt(df.index[val_idx]),
                                }
                                break
                i += 1
            if best:
                results.append(best)

        # ---- SHORT
        df['Ref_Line_Low'] = df['Low'].rolling(window=10).min().shift(1)
        rolling_min_idx = df['Low'].rolling(window=10).apply(lambda x: x.argmin(), raw=True).shift(1)
        ref_val_short = df['Ref_Line_Low'].iloc[val_idx]
        if not pd.isna(ref_val_short) and p_close > ref_val_short:
            best = None
            i = 10
            while i < val_idx:
                r_val = df['Ref_Line_Low'].iloc[i]
                if not pd.isna(r_val) and df['Close'].iloc[i] < r_val:
                    bos_idx = i
                    if (val_idx - bos_idx) <= MAX_DAYS_SINCE_BOS:
                        for j in range(bos_idx + 1, val_idx):
                            if df['High'].iloc[j] > r_val:
                                sl_loc = int(rolling_min_idx.iloc[i]) if not pd.isna(rolling_min_idx.iloc[i]) else (i - 10)
                                best = {
                                    'Ticker': ticker.replace('.NS', ''),
                                    'Direction': 'SHORT',
                                    'BoS_Date': fmt(df.index[bos_idx]),
                                    'Swing_Date': fmt(df.index[i - 10 + sl_loc]),
                                    'Today_Date': fmt(df.index[val_idx]),
                                }
                                break
                i += 1
            if best:
                results.append(best)
        return results
    except Exception as e:
        print(f"  scan error {ticker}: {e}")
        return results


def prefetch(symbols):
    """Fetch history for several symbols in parallel (rate-limited)."""
    todo = [x for x in dict.fromkeys(symbols) if x not in _CACHE]
    if todo:
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(get_df, todo))


def prefilter(symbols):
    """One quote call for all stocks -> keep only those whose latest candle could
    pass the scanner's candle-shape test (slightly looser than the real test; the
    exact test still runs on full history afterwards). Falls back to all symbols."""
    now = datetime.now(IST)
    if now.weekday() < 5 and now.time() < dtime(15, 45):
        return symbols            # today's candle not final yet -> can't use quote
    try:
        keys = [f"NSE:{x}" for x in symbols if x in TOKENS]
        q = {}
        for i in range(0, len(keys), 500):
            q.update(KITE.ohlc(keys[i:i + 500]))
        keep = []
        for x in symbols:
            d = q.get(f"NSE:{x}")
            if not d:
                keep.append(x); continue
            o, h, l = d["ohlc"]["open"], d["ohlc"]["high"], d["ohlc"]["low"]
            c = d["last_price"]
            rng = h - l
            if not (o and h and l and c) or rng <= 0:
                keep.append(x); continue
            body = abs(c - o)
            up, lo = h - max(o, c), min(o, c) - l
            if body / rng <= 0.50 and (up >= 1.8 * body or lo >= 1.8 * body):
                keep.append(x)
        return keep
    except Exception as e:
        print(f"  prefilter skipped ({e}); scanning all")
        return symbols


def run_scan():
    syms = [u.replace('.NS', '') for u in UNIVERSE]
    cand = prefilter(syms)
    print(f"Scanning {len(cand)} of {len(syms)} F&O stocks (rest fail the candle test)...")
    prefetch(cand)
    out = []
    for t in cand:
        out.extend(scan_ticker(t))
    df = pd.DataFrame(out, columns=['Ticker', 'Direction', 'BoS_Date', 'Swing_Date', 'Today_Date'])
    print(f"Found {len(df)} signal(s).")
    return df


# ----------------------------------------------------------------------
# Entry prices -> alerts
# ----------------------------------------------------------------------
def tick_round(x):
    return round(round(x / TICK) * TICK, 2)


def build_alerts(signals):
    """LONG -> swing high / '>=' ; SHORT -> swing low / '<='."""
    alerts, skipped = [], []
    prefetch(list(signals['Ticker']))
    for _, s in signals.iterrows():
        sym, direction = s['Ticker'], str(s['Direction']).upper()
        try:
            raw = get_df(sym)
            if raw is None:
                raise ValueError("no price data")
            day = raw[raw.index.strftime('%Y-%m-%d') == str(s['Swing_Date'])]
            if day.empty:
                raise ValueError(f"swing date {s['Swing_Date']} not in data")
            last_close = float(raw['Close'].iloc[-1])
            if direction == 'LONG':
                level, op = tick_round(float(day['High'].iloc[0])), '>='
                if last_close >= level:
                    raise ValueError(f"already above entry (close {last_close:.2f} >= {level})")
            elif direction == 'SHORT':
                level, op = tick_round(float(day['Low'].iloc[0])), '<='
                if last_close <= level:
                    raise ValueError(f"already below entry (close {last_close:.2f} <= {level})")
            else:
                raise ValueError(f"bad direction {direction}")
            alerts.append({
                'name': f"{PREFIX}{direction}|{sym}|{level:.2f}",
                'symbol': sym, 'direction': direction,
                'operator': op, 'level': level, 'last_close': last_close,
            })
        except Exception as e:
            skipped.append((sym, direction, str(e)))
    return alerts, skipped


# ----------------------------------------------------------------------
# Kite alerts API
# ----------------------------------------------------------------------
class KiteAlerts:
    BASE = "https://api.kite.trade/alerts"

    def __init__(self, api_key, access_token):
        self.h = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}

    def list(self):
        seen, out, page = set(), [], 1
        while page <= 20:
            r = requests.get(self.BASE, headers=self.h,
                             params={"page": page, "page_size": 100}, timeout=30)
            r.raise_for_status()
            rows = r.json().get("data") or []
            new = [a for a in rows if a["uuid"] not in seen]
            if not new:
                break
            seen.update(a["uuid"] for a in new)
            out.extend(new)
            if len(rows) < 100:
                break
            page += 1
        return out

    def create(self, a):
        r = requests.post(self.BASE, headers=self.h, timeout=30, data={
            "name": a['name'], "type": "simple",
            "lhs_exchange": "NSE", "lhs_tradingsymbol": a['symbol'],
            "lhs_attribute": "LastTradedPrice", "operator": a['operator'],
            "rhs_type": "constant", "rhs_constant": a['level'],
        })
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        return r.json()["data"]["uuid"]

    def delete(self, uuids):
        for i in range(0, len(uuids), 20):
            r = requests.delete(self.BASE, headers=self.h,
                                params=[("uuid", u) for u in uuids[i:i + 20]], timeout=30)
            r.raise_for_status()


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="use an existing signals CSV instead of scanning")
    ap.add_argument("--dry-run", action="store_true", help="print only, do not create/delete alerts")
    args = ap.parse_args()

    kite, api_key, access_token = kite_login()
    print("Kite login OK.")
    init_prices(kite)

    signals = pd.read_csv(args.csv) if args.csv else run_scan()
    if signals.empty:
        print("No signals - nothing to do (existing SWG alerts left untouched).")
        return 0

    alerts, skipped = build_alerts(signals)
    print(f"\n{len(alerts)} alert(s) to place:")
    for a in alerts:
        arrow = "crosses UP  " if a['direction'] == 'LONG' else "crosses DOWN"
        print(f"  {a['direction']:5} {a['symbol']:12} LTP {a['operator']} {a['level']:>10.2f}  "
              f"({arrow}; last close {a['last_close']:.2f})")
    if skipped:
        print(f"\nSkipped {len(skipped)}:")
        for sym, d, why in skipped:
            print(f"  {d:5} {sym:12} {why}")

    if args.dry_run:
        print("\nDry run - no alerts placed.")
        return 0

    alerts_api = KiteAlerts(api_key, access_token)
    existing = [x for x in alerts_api.list() if x.get("name", "").startswith(PREFIX)
                and x.get("status") != "deleted"]
    existing_names = {x["name"] for x in existing}
    wanted_names = {a['name'] for a in alerts}

    stale = [x["uuid"] for x in existing
             if x["name"] not in wanted_names and x.get("status") == "enabled"]
    if stale:
        alerts_api.delete(s
