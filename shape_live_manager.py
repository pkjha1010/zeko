"""
SHAPELIVE MANAGER  -  runs ONE job each trading morning, about 09:00 IST to 10:03 IST.

The ATO alerts made by shape_live_plan.py (06:00) do the ENTRY on Zerodha's servers (BUY option, overnight NRML,
validity DAY) and carry the built-in 10% stop-loss (GTT). This manager only does the time rules:

  entry placed before 09:25:00           -> square off at 09:30:00 sharp
  entry placed 09:25:00 .. B_END (09:50) -> square off at 10:00:00 sharp
  entry placed after B_END               -> sold immediately
  at B_END (09:50:00)                    -> every ATO alert still waiting (SHP|) is deleted: no new entries

Square off = delete the position's stop-loss GTT (so it cannot fire after we are out), check the open quantity,
sell at market (with market protection). If the stop-loss already closed the position nothing is sold.
Because the product is NRML (overnight) a position left open would stay open overnight - so the job also closes
everything still open at the end of the run.
DRY_RUN=1 prints every action and sends nothing.
"""
import os, sys, json, time, threading, requests, pyotp
from datetime import datetime, time as dtime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect

PFX = "SHP|"
PLAN = os.environ.get("PLAN_FILE") or "live_plan/plan.json"
DRY_RUN = (os.environ.get("DRY_RUN") or "0") == "1"
PRODUCT = os.environ.get("PRODUCT") or "NRML"


def hhmmss(s):
    h, m, *sec = [int(x) for x in s.split(":")]
    return dtime(h, m, sec[0] if sec else 0)


A_END = hhmmss(os.environ.get("A_END") or "09:25:00")      # entries before this exit at EXIT_A
B_END = hhmmss(os.environ.get("B_END") or "09:50:00")      # last allowed entry
EXIT_A = hhmmss(os.environ.get("EXIT_A") or "09:30:00")
EXIT_B = hhmmss(os.environ.get("EXIT_B") or "10:00:00")
JOB_END = hhmmss(os.environ.get("JOB_END") or "10:03:00")
HOLIDAY_CHECK = hhmmss("09:20:00")
POLL_S = float(os.environ.get("POLL_S") or 0.3)
STOP_WAIT_S = float(os.environ.get("STOP_WAIT_S") or 0.5)       # a stop-loss SELL still unfilled after this many seconds is cancelled and sold at market
ENTRY_WAIT_S = float(os.environ.get("ENTRY_WAIT_S") or 0.5)   # a BUY order still unfilled after this many seconds is cancelled
ORDER_PROXY = (os.environ.get("ORDER_PROXY") or "").strip()    # static-IP proxy, e.g. http://user:pass@host:port  (orders only)
PROXIES = {"http": ORDER_PROXY, "https": ORDER_PROXY} if ORDER_PROXY else None

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
            kite = KiteConnect(api_key=key)
            d = kite.generate_session(get_request_token(key, uid, pwd, tk), api_secret=sec)
            kite.set_access_token(d["access_token"])
            log("Kite login OK"); return kite, key, d["access_token"]
        except Exception as e:
            last = e; log(f"login attempt {i}/{tries} failed: {e}"); time.sleep(5)
    raise last


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
                if PROXIES and method != "GET" and path.startswith("/orders"): kw["proxies"] = PROXIES     # only order calls need the whitelisted IP
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


def tick(x):
    return round(round(x / 0.05) * 0.05, 2)


def as_dt(v):
    if v is None or v == "": return None
    if isinstance(v, datetime): return v.replace(tzinfo=None)
    try: return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception: return None


class Manager:
    def __init__(self, k, plan):
        self.k, self.lock = k, threading.RLock()
        self.items = {i["option"]: i for i in plan["items"]}
        self.seen, self.trades = set(), {}              # order ids handled; option -> trade dict
        self.cutoff_done, self.holiday_checked, self.stop = False, False, False
        self.log_rows = []
        self.open_seen, self.cancel_tries, self.stop_handled = {}, {}, set()

    # ------------------------------------------------------------ orders (REST)
    def place(self, **p):
        if DRY_RUN: log(f"DRY order {p}"); return "DRY"
        t0 = time.time()
        ok, d = self.k.call("POST", "/orders/regular", tries=3, data=p)
        log(f"order {p['transaction_type']} {p['order_type']} {p['tradingsymbol']} -> {'OK ' + str(d.get('order_id')) if ok else 'FAILED ' + str(d)} ({(time.time() - t0) * 1000:.0f} ms)")
        return d["order_id"] if ok else None

    def sell_market(self, sym, qty, tag):
        return self.place(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="SELL", quantity=int(qty),
                          product=PRODUCT, order_type="MARKET", validity="DAY", market_protection=-1, tag=tag)

    # ------------------------------------------------------------ a BUY fill is noticed
    def on_order(self, o):
        try:
            sym = o.get("tradingsymbol")
            if sym not in self.items or o.get("transaction_type") != "BUY": return
            st = o.get("status")
            if not (st == "COMPLETE" or (st == "CANCELLED" and int(o.get("filled_quantity") or 0) > 0)): return
            oid = str(o.get("order_id"))
            with self.lock:
                if oid in self.seen: return
                self.seen.add(oid)
            self.entered(sym, o)
        except Exception as e:
            log(f"on_order error: {e!r}")

    def entered(self, sym, o):
        qty = int(o.get("filled_quantity") or self.items[sym]["qty"])
        px = float(o.get("average_price") or 0)
        ft = as_dt(o.get("order_timestamp")) or as_dt(o.get("exchange_timestamp")) or now_ist()      # PLACED time decides the variant
        t = ft.time()
        if px <= 0: px = float(self.items[sym]["close_price"]); log(f"{sym}: fill price missing, using close {px}")
        tr = dict(sym=sym, qty=qty, entry=px, fill=ft, closed=False, exit_at=None, variant=None)
        with self.lock: self.trades[sym] = tr
        if t > B_END:
            tr["variant"] = "LATE"; log(f"{sym}: entry at {ft:%H:%M:%S} is after {B_END} -> selling at once")
            self.close_now(tr, "SHPLATE"); return
        tr["variant"] = "A" if t < A_END else "B"
        tr["exit_at"] = EXIT_A if tr["variant"] == "A" else EXIT_B
        log(f"{sym}: ENTRY variant {tr['variant']} {qty} @ {px} placed {ft:%H:%M:%S} -> square off at {tr['exit_at']}")

    # ------------------------------------------------------------ exits
    def open_qty(self, sym):
        if DRY_RUN: return self.trades[sym]["qty"]
        ok, d = self.k.call("GET", "/portfolio/positions", tries=3)
        if not ok: return None
        for p in d.get("net", []):
            if p["tradingsymbol"] == sym and p["product"] == PRODUCT: return int(p["quantity"])
        return 0

    def drop_stoploss(self, sym):
        """Delete the ATO's built-in stop-loss (a GTT) for this option so it cannot trigger after we square off."""
        if DRY_RUN: log(f"DRY delete active GTTs of {sym}"); return
        ok, rows = self.k.call("GET", "/gtt/triggers", tries=2)
        if not ok: log(f"{sym}: cannot list GTTs: {rows}"); return
        n = 0
        for g in rows or []:
            if g.get("status") == "active" and (g.get("condition") or {}).get("tradingsymbol") == sym:
                ok2, d = self.k.call("DELETE", f"/gtt/triggers/{g['id']}", tries=2); n += ok2
                if not ok2: log(f"{sym}: GTT {g['id']} delete failed: {d}")
        log(f"{sym}: stop-loss GTT removed: {n}")

    OPEN_SELL_STATES = ("OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "MODIFY PENDING", "OPEN PENDING")

    def cancel_open_sells(self, sym):
        """Cancel every waiting SELL order of this option. A stop-loss limit order that the price ran past stays open and RESERVES the
        quantity, so a new sell is refused ('not sufficient margin') until it is cancelled."""
        if DRY_RUN: log(f"DRY cancel open SELL orders of {sym}"); return 0
        ok, rows = self.k.call("GET", "/orders", tries=2)
        n = 0
        for o in (rows or []) if ok else []:
            if (o.get("tradingsymbol") == sym and o.get("transaction_type") == "SELL" and o.get("product") == PRODUCT
                    and str(o.get("status", "")).upper() in self.OPEN_SELL_STATES):
                ok2, d = self.k.call("DELETE", f"/orders/regular/{o['order_id']}", tries=2); n += int(ok2)
                log(f"{sym}: waiting SELL {o['order_id']} ({o.get('order_type')} {o.get('price')}) -> cancel " + ("OK" if ok2 else f"failed: {d}"))
        if n: time.sleep(0.15)
        return n

    def flatten(self, sym, fallback_qty, tag, tries=4):
        """Make the position zero: cancel waiting sells, sell what is open at market, check it really went flat, repeat if not."""
        for attempt in range(1, tries + 1):
            self.cancel_open_sells(sym)
            q = self.open_qty(sym)
            if q is None: q = fallback_qty; log(f"{sym}: positions unavailable, selling stored qty {q}")
            if q <= 0: log(f"{sym}: nothing open" + (" (stop-loss already closed it)" if attempt == 1 else " - flat")); return True
            oid = self.sell_market(sym, q, tag)
            if oid == "DRY": return True
            if not oid: time.sleep(0.3); continue
            t0 = time.time()
            while time.time() - t0 < 2.0:                               # wait for the fill; a market order can turn into a waiting limit order
                time.sleep(0.3)
                if self.open_qty(sym) == 0: log(f"{sym}: position closed (attempt {attempt})"); return True
            log(f"{sym}: still open after sell attempt {attempt}/{tries} - cancelling the waiting order and selling again")
        log(f"{sym}: !!! COULD NOT CLOSE the position - close it by hand in Kite"); return False

    def close_now(self, tr, tag):
        with self.lock:
            if tr["closed"]: return
            tr["closed"] = True
        t0 = time.time()
        self.drop_stoploss(tr["sym"])
        self.flatten(tr["sym"], tr["qty"], tag)
        log(f"{tr['sym']}: exit sequence done in {(time.time() - t0) * 1000:.0f} ms")

    def due_exits(self):
        t = now_ist().time()
        with self.lock:
            due = [tr for tr in self.trades.values() if not tr["closed"] and tr["exit_at"] and t >= tr["exit_at"]]
        for tr in due: threading.Thread(target=self.close_now, args=(tr, "SHPEXIT"), daemon=True).start()

    # ------------------------------------------------------------ alerts / market checks
    def delete_alerts(self):
        ok, rows = self.k.call("GET", "/alerts", tries=3, params={"status": "enabled", "page_size": 100})
        ids = [a["uuid"] for a in (rows or []) if str(a.get("name", "")).startswith(PFX) and a.get("status") == "enabled"] if ok else []
        if DRY_RUN: log(f"DRY delete {len(ids)} waiting alerts"); return
        for i in range(0, len(ids), 50): self.k.call("DELETE", "/alerts", tries=3, params=[("uuid", u) for u in ids[i:i + 50]])
        log(f"deleted {len(ids)} waiting SHP| alerts - no new entries")

    def market_open_today(self):
        ok, d = self.k.call("GET", "/quote", tries=3, params=[("i", "NSE:NIFTY 50")])
        if not ok: return True                                   # unknown: assume open (safer to keep managing)
        lt = as_dt(d["NSE:NIFTY 50"].get("last_trade_time"))
        return bool(lt and lt.date() == now_ist().date())

    OPEN_STATES = ("OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "MODIFY PENDING", "OPEN PENDING")

    def watch_open(self, o):
        """A plan BUY order that is still unfilled ENTRY_WAIT_S after we first see it is cancelled (price ran away)."""
        if o.get("tradingsymbol") not in self.items or o.get("transaction_type") != "BUY": return
        if o.get("product") != PRODUCT or str(o.get("status", "")).upper() not in self.OPEN_STATES: return
        oid = str(o.get("order_id"))
        first = self.open_seen.setdefault(oid, time.time())
        if time.time() - first < ENTRY_WAIT_S or self.cancel_tries.get(oid, 0) >= 5: return
        self.cancel_tries[oid] = self.cancel_tries.get(oid, 0) + 1
        sym = o["tradingsymbol"]
        if DRY_RUN: log(f"DRY cancel unfilled BUY {sym} {oid}"); self.cancel_tries[oid] = 99; return
        ok, d = self.k.call("DELETE", f"/orders/regular/{oid}", tries=1)
        log(f"{sym}: BUY {oid} still unfilled after {ENTRY_WAIT_S}s -> cancel " + ("OK" if ok else f"not done ({d}) - may have filled just now"))

    def watch_sell(self, o):
        """The stop-loss (a GTT) sells with a LIMIT order. If the price already ran past it the order just waits and the position stays open.
        A waiting SELL of a managed option that is not ours is cancelled after STOP_WAIT_S and the position is sold at market."""
        sym = o.get("tradingsymbol")
        if o.get("transaction_type") != "SELL" or o.get("product") != PRODUCT or str(o.get("status", "")).upper() not in self.OPEN_SELL_STATES: return
        if str(o.get("tag") or "").startswith("SHP"): return
        tr = self.trades.get(sym)
        if not tr or tr["closed"]: return
        oid = str(o.get("order_id")); first = self.open_seen.setdefault(oid, time.time())
        if time.time() - first < STOP_WAIT_S or oid in self.stop_handled: return
        self.stop_handled.add(oid)
        log(f"{sym}: stop-loss SELL {oid} still waiting after {STOP_WAIT_S}s (price ran past it) -> cancel and sell at market")
        threading.Thread(target=self.close_now, args=(tr, "SHPSTOP"), daemon=True).start()

    def poll(self):
        ok, rows = self.k.call("GET", "/orders", tries=1)
        if ok:
            for o in rows or []:
                self.on_order(o); self.watch_open(o); self.watch_sell(o)

    def run(self):
        last_poll = 0.0
        while not self.stop:
            now = now_ist(); t = now.time()
            if t >= JOB_END: break
            if not self.holiday_checked and t >= HOLIDAY_CHECK:
                self.holiday_checked = True
                if not self.market_open_today():
                    log("No trading today (NIFTY has no trade yet) - leaving alerts untouched and stopping"); return
            if not self.cutoff_done and t >= B_END:
                self.cutoff_done = True; threading.Thread(target=self.delete_alerts, daemon=True).start()
            self.due_exits()
            if time.time() - last_poll >= POLL_S and t >= dtime(9, 14, 30):
                last_poll = time.time(); self.poll()
            near = any((not tr["closed"]) and tr["exit_at"] and 0 <= (datetime.combine(now.date(), tr["exit_at"]) - now).total_seconds() < 2
                       for tr in list(self.trades.values()))
            sleep(0.005 if near else 0.1)
        # last check: anything still open is closed
        for tr in list(self.trades.values()):
            if not tr["closed"]: log(f"{tr['sym']}: still open at job end -> closing"); self.close_now(tr, "SHPEND")
        for sym, tr in list(self.trades.items()):                       # final sweep: no stop-loss left behind, nothing left open
            self.drop_stoploss(sym)
            if tr["closed"] and (self.open_qty(sym) or 0) > 0: log(f"{sym}: position still open at job end -> closing"); self.flatten(sym, tr["qty"], "SHPEND")
        log(f"done. entries seen: {len(self.trades)}")


def sleep(s): time.sleep(s)


def start_ticker(mgr, key, tok):
    try:
        from kiteconnect import KiteTicker
        kws = KiteTicker(key, tok)
        kws.on_order_update = lambda ws, data: mgr.on_order(data)
        kws.on_connect = lambda ws, r: log("order WebSocket connected")
        kws.on_error = lambda ws, c, r: log(f"WebSocket error {c} {r}")
        kws.connect(threaded=True)
        return kws
    except Exception as e:
        log(f"WebSocket not started ({e!r}) - using polling only"); return None


def cleanup_now():
    """Delete every waiting SHP| alert right now (manual clean-up / test of the delete step)."""
    kite, key, tok = login(); k = K(key, tok)
    Manager(k, {"items": []}).delete_alerts()


def iptest():
    """Safe check of the static IP: sends a deliberately invalid order. 'No IPs configured' = IP not accepted;
    any other (validation) error = the IP passed Zerodha's check and nothing was placed."""
    log(f"order proxy: {'ON' if PROXIES else 'OFF (direct)'}")
    try:
        r = requests.get("https://api.ipify.org", timeout=15, proxies=PROXIES); log(f"orders leave from IP: {r.text.strip()}")
    except Exception as e: log(f"could not read the proxy IP: {e!r}")
    kite, key, tok = login(); k = K(key, tok)
    ok, d = k.call("POST", "/orders/regular", tries=1, data=dict(variety="regular", exchange="NFO", tradingsymbol="IPTEST00INVALID", transaction_type="BUY",
                   quantity=1, product=PRODUCT, order_type="MARKET", validity="DAY", market_protection=-1))
    msg = str(d)
    if ok: log(f"UNEXPECTED: order accepted {d} - cancel it in Kite!")
    elif "ip" in msg.lower() and ("no ips" in msg.lower() or "allowed" in msg.lower() or "whitelist" in msg.lower()): log(f"IP REJECTED: {msg}")
    else: log(f"IP OK (Zerodha reached order validation, nothing placed): {msg}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup": return cleanup_now()
    if len(sys.argv) > 1 and sys.argv[1] == "iptest": return iptest()
    t = now_ist().time()
    if t >= JOB_END or t < dtime(8, 0):
        log(f"It is {t:%H:%M:%S}. The manager only works between 09:00 and {JOB_END} IST (alerts are deleted at {B_END}, "
            f"square-offs at {EXIT_A} and {EXIT_B}). Outside that window it does nothing. To delete waiting alerts right now use mode=cleanup.")
        return
    if not os.path.exists(PLAN): sys.exit(f"No plan file {PLAN}")
    plan = json.load(open(PLAN))
    today = str(now_ist().date())
    if plan.get("trade_day") and plan["trade_day"] < today:
        sys.exit(f"Plan is for {plan['trade_day']} (older than today {today}) - the evening planner did not run. Nothing to manage.")
    if not plan["items"]: log("plan has no orders today - nothing to manage"); return
    log(f"plan {plan['trade_day']}: " + ", ".join(f"{i['stock']} {i['direction']} {i['qty']}x{i['option']}" for i in plan["items"]))
    kite, key, tok = login(); k = K(key, tok)
    m = Manager(k, plan)
    if os.environ.get("USE_WS") == "1": start_ticker(m, key, tok)   # free plan: the order WebSocket answers 403, polling is used instead
    m.run()


if __name__ == "__main__":
    main()
