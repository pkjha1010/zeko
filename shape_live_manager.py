"""
SHAPELIVE MANAGER  -  runs ONE job each trading morning, about 09:00 IST to 09:45 IST.

Two strategies are traded (shape_live_plan.py makes one ATO alert per setup): SHAPECMP (no shape rule) and BPB (breakout).
The ATO alerts do the ENTRY on Zerodha's servers (BUY option, overnight NRML, validity DAY) and carry the built-in 10% stop-loss (GTT).
This manager only does the time rules and the clean-up:

  09:15 - ENTRY_END (09:26:00)   entries are allowed (the last entry minute is 09:25).
                                 A BUY order that is still unfilled ENTRY_WAIT_S (0.5 s) after we see it is cancelled (price ran away).
  ENTRY_END  09:26:00            every ATO alert still waiting (SHP|) is deleted and every waiting BUY order is cancelled: no new entries.
  all the time                   a stop-loss SELL (the GTT sells with a LIMIT order) that is still waiting STOP_WAIT_S (0.5 s) after we see it
                                 means price ran past it: the order is cancelled and the open position is sold at market.
  EXIT_AT    09:30:00            every position is squared off at 09:30:00 sharp (stop-loss GTT deleted first, waiting orders cancelled, then sold).
  after 09:30:00                 SWEEP, repeated every second until two passes in a row find nothing: waiting alerts, waiting orders (BUY and SELL),
                                 active stop-loss GTTs and open positions of this strategy are cancelled / deleted / closed.  A close order is
                                 MARKET first; if the position is still open the order is cancelled and an aggressive LIMIT through the best
                                 bid/ask is sent, a little more aggressive every attempt, so the position goes flat "by hook or by crook".
                                 If anything is still left at SWEEP_END (09:45) the job ends with an ERROR so you get the GitHub failure mail.
  Scope of the sweep: EVERY open NFO position and EVERY waiting NFO order of the account (SWEEP_ALL=1, the default - this account trades only this
  strategy), plus every waiting SHP| alert.  SWEEP_ALL=0 narrows it to the options of today's plan + orders tagged SHP* (use it if you ever
  run another strategy in the same account).  Other alerts (SWG|...) are never deleted.
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


ENTRY_END = hhmmss(os.environ.get("ENTRY_END") or "09:26:00")   # entries through 09:25:59; at this time waiting alerts are deleted, waiting BUYs cancelled
EXIT_AT = hhmmss(os.environ.get("EXIT_AT") or "09:30:00")       # square off everything
JOB_END = hhmmss(os.environ.get("JOB_END") or "09:45:00")       # the sweep must be clean by now, otherwise the job fails
HOLIDAY_CHECK = hhmmss("09:20:00")
POLL_S = float(os.environ.get("POLL_S") or 0.3)
SWEEP_GAP_S = float(os.environ.get("SWEEP_GAP_S") or 1.0)       # pause between two sweep passes (a market order needs a moment to fill before we judge it)
SWEEP_ALL = (os.environ.get("SWEEP_ALL") or "1") == "1"         # 1 (default) = sweep EVERY NFO position / waiting NFO order of the account; 0 = only this strategy's
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
    # an order in one of these states is still waiting at Zerodha / the exchange
    OPEN_STATES = ("OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "MODIFY VALIDATION PENDING", "MODIFY PENDING", "OPEN PENDING", "AMO REQ RECEIVED")
    OPEN_SELL_STATES = OPEN_STATES

    def __init__(self, k, plan):
        self.k, self.lock = k, threading.RLock()
        self.items = {}
        for i in plan["items"]:                         # SHAPECMP and BPB may pick the same option: one entry, quantities added
            j = self.items.setdefault(i["option"], dict(i, qty=0)); j["qty"] += int(i["qty"])
        self.seen, self.trades = set(), {}              # order ids handled; option -> trade dict
        self.entry_closed, self.holiday_checked, self.stop = False, False, False
        self.open_seen, self.cancel_tries, self.stop_handled = {}, {}, set()
        self.busy = set()                               # options whose position is being closed right now (one closer at a time)

    # ------------------------------------------------------------ scope: what belongs to this strategy
    def in_scope(self, row):
        """An order / position row of today's plan options or tagged SHP*.  SWEEP_ALL=1: every NFO row."""
        if SWEEP_ALL and row.get("exchange") == "NFO": return True
        return row.get("tradingsymbol") in self.items or str(row.get("tag") or "").startswith("SHP")

    def claim(self, sym):
        with self.lock:
            if sym in self.busy: return False
            self.busy.add(sym); return True

    def release(self, sym):
        with self.lock: self.busy.discard(sym)

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
        ft = as_dt(o.get("order_timestamp")) or as_dt(o.get("exchange_timestamp")) or now_ist()      # PLACED time decides
        if px <= 0: px = float(self.items[sym]["close_price"]); log(f"{sym}: fill price missing, using close {px}")
        late = ft.time() >= ENTRY_END
        with self.lock:
            tr = self.trades.get(sym)
            if tr and not tr["closed"]:                 # a second fill of the same option (other strategy, partial fill): average in
                tot = tr["qty"] + qty; tr["entry"] = (tr["entry"] * tr["qty"] + px * qty) / tot; tr["qty"] = tot
                log(f"{sym}: another fill {qty} @ {px} -> total {tot} @ {tr['entry']:.2f}")
                return
            tr = dict(sym=sym, qty=qty, entry=px, fill=ft, closed=False, exit_at=EXIT_AT, variant="LATE" if late else "ENTRY")
            self.trades[sym] = tr
        if late:
            log(f"{sym}: entry placed {ft:%H:%M:%S} is after {ENTRY_END} -> selling at once")
            self.close_now(tr, "SHPLATE"); return
        log(f"{sym}: ENTRY {qty} @ {px} placed {ft:%H:%M:%S} -> square off at {EXIT_AT}")

    # ------------------------------------------------------------ positions, stop-loss GTT, waiting orders
    def positions(self):
        """{option: [open legs]} of every non-zero net position (a leg = qty with sign, product, exchange), or None if Kite could not be read."""
        ok, d = self.k.call("GET", "/portfolio/positions", tries=3)
        if not ok or not isinstance(d, dict): return None
        out = {}
        for p in d.get("net", []) or []:
            q = int(p.get("quantity") or 0)
            if q: out.setdefault(p["tradingsymbol"], []).append(dict(tradingsymbol=p["tradingsymbol"], qty=q, product=p.get("product") or PRODUCT, exchange=p.get("exchange") or "NFO"))
        return out

    def open_qty(self, sym):
        if DRY_RUN: return self.trades[sym]["qty"]
        pos = self.positions()
        return None if pos is None else sum(l["qty"] for l in pos.get(sym, []))

    def drop_stoploss(self, sym):
        """Delete the ATO's built-in stop-loss (a GTT) for this option so it cannot trigger after we square off."""
        if DRY_RUN: log(f"DRY delete active GTTs of {sym}"); return 0
        ok, rows = self.k.call("GET", "/gtt/triggers", tries=2)
        if not ok: log(f"{sym}: cannot list GTTs: {rows}"); return 0
        n = 0
        for g in rows or []:
            if g.get("status") == "active" and (g.get("condition") or {}).get("tradingsymbol") == sym:
                ok2, d = self.k.call("DELETE", f"/gtt/triggers/{g['id']}", tries=2); n += ok2
                if not ok2: log(f"{sym}: GTT {g['id']} delete failed: {d}")
        log(f"{sym}: stop-loss GTT removed: {n}")
        return n

    def cancel_waiting(self, sym=None, side=None):
        """Cancel waiting orders.  sym given: every waiting order of that option (both sides - a stop-loss limit order that price ran past RESERVES the
        quantity, so a new sell is refused until it is cancelled).  sym None: every waiting order in scope.  side limits it to BUY or SELL."""
        ok, rows = self.k.call("GET", "/orders", tries=2)
        if not ok: log(f"cannot read the order book: {rows}"); return 0
        n = 0
        for o in rows or []:
            if str(o.get("status", "")).upper() not in self.OPEN_STATES: continue
            if sym and o.get("tradingsymbol") != sym: continue
            if side and o.get("transaction_type") != side: continue
            if not sym and not self.in_scope(o): continue
            if DRY_RUN: log(f"DRY cancel waiting {o.get('transaction_type')} {o.get('tradingsymbol')} {o.get('order_id')}"); continue
            ok2, d = self.k.call("DELETE", f"/orders/{o.get('variety') or 'regular'}/{o['order_id']}", tries=2); n += int(ok2)
            log(f"{o.get('tradingsymbol')}: waiting {o.get('transaction_type')} {o['order_id']} ({o.get('order_type')} {o.get('price')}) -> cancel " + ("OK" if ok2 else f"failed: {d}"))
        if n: time.sleep(0.15)
        return n

    def quote(self, sym):
        ok, d = self.k.call("GET", "/quote", tries=2, params=[("i", f"NFO:{sym}")])
        return ((d or {}).get(f"NFO:{sym}") or {}) if ok else {}

    def close_order(self, leg, tag, attempt):
        """One order that closes one open leg (qty > 0 -> SELL, qty < 0 -> BUY).  Attempts 1-2: MARKET with automatic market protection.
        From attempt 3: a LIMIT priced THROUGH the best bid (SELL) / ask (BUY) by 3 % x (attempt - 2), so it fills even when the market order
        keeps turning into a limit order that waits."""
        sym, q = leg["tradingsymbol"], abs(int(leg["qty"])); side = "SELL" if leg["qty"] > 0 else "BUY"
        common = dict(variety="regular", exchange=leg["exchange"], tradingsymbol=sym, transaction_type=side, quantity=q, product=leg["product"], validity="DAY", tag=tag)
        if attempt > 2 and not DRY_RUN:
            qt = self.quote(sym); depth = qt.get("depth") or {}
            best = float((((depth.get("buy" if side == "SELL" else "sell")) or [{}])[0]).get("price") or 0) or float(qt.get("last_price") or 0)
            if best > 0:
                slip = 0.03 * (attempt - 2)
                price = tick(max(0.05, best * (1 - slip))) if side == "SELL" else tick(best * (1 + slip))
                return self.place(order_type="LIMIT", price=price, **common)
            log(f"{sym}: no quote for an aggressive limit price - market order again")
        return self.place(order_type="MARKET", market_protection=-1, **common)

    def flatten(self, sym, fallback_qty, tag, tries=6):
        """Make the position zero: cancel every waiting order of the option, close what is open, check it really went flat, repeat if not."""
        for attempt in range(1, tries + 1):
            if not DRY_RUN: self.cancel_waiting(sym)
            if DRY_RUN: legs = [dict(tradingsymbol=sym, qty=fallback_qty, product=PRODUCT, exchange="NFO")] if fallback_qty else []
            else:
                pos = self.positions()
                if pos is None: legs = [dict(tradingsymbol=sym, qty=fallback_qty, product=PRODUCT, exchange="NFO")] if fallback_qty else []; log(f"{sym}: positions unavailable, using stored qty {fallback_qty}")
                else: legs = pos.get(sym, [])
            if not legs: log(f"{sym}: nothing open" + (" (stop-loss already closed it)" if attempt == 1 else " - flat")); return True
            sent = [self.close_order(l, tag, attempt) for l in legs]
            if "DRY" in sent: return True
            if not all(sent): time.sleep(0.3); continue
            t0 = time.time()
            while time.time() - t0 < 2.0:                               # wait for the fill; a market order can turn into a waiting limit order
                time.sleep(0.3)
                pos = self.positions()
                if pos is not None and not pos.get(sym): log(f"{sym}: position closed (attempt {attempt})"); return True
            log(f"{sym}: still open after close attempt {attempt}/{tries} - cancelling the waiting order and closing again, more aggressively")
        log(f"{sym}: !!! COULD NOT CLOSE the position - close it by hand in Kite"); return False

    def close_now(self, tr, tag):
        with self.lock:
            if tr["closed"]: return
            tr["closed"] = True
        if not self.claim(tr["sym"]): return                           # the sweep is already closing this option
        try:
            t0 = time.time()
            self.drop_stoploss(tr["sym"])
            self.flatten(tr["sym"], tr["qty"], tag)
            log(f"{tr['sym']}: exit sequence done in {(time.time() - t0) * 1000:.0f} ms")
        finally:
            self.release(tr["sym"])

    def close_untracked(self, sym, tag):
        """A position / waiting stop-loss of a plan option whose BUY we never noticed: same exit sequence."""
        if not self.claim(sym): return
        try:
            self.drop_stoploss(sym); self.flatten(sym, 0, tag)
        finally:
            self.release(sym)

    def due_exits(self):
        t = now_ist().time()
        with self.lock:
            due = [tr for tr in self.trades.values() if not tr["closed"] and t >= tr["exit_at"]]
        for tr in due: threading.Thread(target=self.close_now, args=(tr, "SHPEXIT"), daemon=True).start()

    # ------------------------------------------------------------ alerts / market checks
    def delete_alerts(self):
        """Delete every waiting SHP| alert (pending ATO).  Returns how many were found."""
        ok, rows = self.k.call("GET", "/alerts", tries=3, params={"status": "enabled", "page_size": 100})
        ids = [a["uuid"] for a in (rows or []) if str(a.get("name", "")).startswith(PFX) and a.get("status") == "enabled"] if ok else []
        if DRY_RUN: log(f"DRY delete {len(ids)} waiting alerts"); return 0
        for i in range(0, len(ids), 50): self.k.call("DELETE", "/alerts", tries=3, params=[("uuid", u) for u in ids[i:i + 50]])
        if ids: log(f"deleted {len(ids)} waiting SHP| alerts - no new entries")
        return len(ids)

    def close_entries(self):
        """ENTRY_END reached: no new entries - waiting alerts deleted, waiting BUY orders cancelled."""
        self.delete_alerts()
        n = self.cancel_waiting(side="BUY")
        log(f"entry time over ({ENTRY_END}): waiting BUY orders cancelled: {n}")

    def market_open_today(self):
        ok, d = self.k.call("GET", "/quote", tries=3, params=[("i", "NSE:NIFTY 50")])
        if not ok: return True                                   # unknown: assume open (safer to keep managing)
        lt = as_dt(d["NSE:NIFTY 50"].get("last_trade_time"))
        return bool(lt and lt.date() == now_ist().date())

    def watch_open(self, o):
        """A BUY order of ours that is still unfilled ENTRY_WAIT_S after we first see it is cancelled (price ran away) - at once after ENTRY_END."""
        if o.get("transaction_type") != "BUY" or str(o.get("status", "")).upper() not in self.OPEN_STATES or not self.in_scope(o): return
        oid = str(o.get("order_id"))
        first = self.open_seen.setdefault(oid, time.time())
        late = now_ist().time() >= ENTRY_END
        if (not late and time.time() - first < ENTRY_WAIT_S) or self.cancel_tries.get(oid, 0) >= 8: return
        self.cancel_tries[oid] = self.cancel_tries.get(oid, 0) + 1
        sym = o.get("tradingsymbol")
        if DRY_RUN: log(f"DRY cancel unfilled BUY {sym} {oid}"); self.cancel_tries[oid] = 99; return
        ok, d = self.k.call("DELETE", f"/orders/{o.get('variety') or 'regular'}/{oid}", tries=1)
        log(f"{sym}: BUY {oid} still unfilled -> cancel " + ("OK" if ok else f"not done ({d}) - may have filled just now"))

    def watch_sell(self, o):
        """The stop-loss (a GTT) sells with a LIMIT order. If the price already ran past it the order just waits and the position stays open.
        A waiting SELL of a plan option that is not ours is cancelled after STOP_WAIT_S and the position is sold at market."""
        sym = o.get("tradingsymbol")
        if o.get("transaction_type") != "SELL" or str(o.get("status", "")).upper() not in self.OPEN_STATES or not self.in_scope(o): return
        if str(o.get("tag") or "").startswith("SHP"): return          # our own close orders are handled inside flatten()
        tr = self.trades.get(sym)
        if tr and tr["closed"]: return
        oid = str(o.get("order_id")); first = self.open_seen.setdefault(oid, time.time())
        if time.time() - first < STOP_WAIT_S or oid in self.stop_handled: return
        self.stop_handled.add(oid)
        log(f"{sym}: stop-loss SELL {oid} still waiting after {STOP_WAIT_S}s (price ran past it) -> cancel and sell at market")
        if tr: threading.Thread(target=self.close_now, args=(tr, "SHPSTOP"), daemon=True).start()
        else: threading.Thread(target=self.close_untracked, args=(sym, "SHPSTOP"), daemon=True).start()

    def poll(self):
        ok, rows = self.k.call("GET", "/orders", tries=1)
        if ok:
            for o in rows or []:
                self.on_order(o); self.watch_open(o); self.watch_sell(o)

    # ------------------------------------------------------------ the 09:30 sweep
    def sweep(self):
        """One clean-up pass.  Returns True when nothing of this strategy was left: no waiting alert, no waiting order, no active stop-loss GTT, no open position."""
        if self.busy: return False                                      # an exit is still running - judge again next pass
        dirty = False
        if self.delete_alerts(): dirty = True                          # 1. pending ATO alerts
        if self.cancel_waiting(): dirty = True                         # 2. waiting orders, BUY and SELL
        pos = self.positions()
        if pos is None: log("sweep: positions could not be read - will check again"); return False
        syms = sorted({s for s in self.items} | {s for s, legs in pos.items() if any(self.in_scope(l) for l in legs)})
        if not DRY_RUN:                                                # 3. stop-loss GTTs of those options
            ok, rows = self.k.call("GET", "/gtt/triggers", tries=2)
            for g in (rows or []) if ok else []:
                if g.get("status") == "active" and (g.get("condition") or {}).get("tradingsymbol") in syms:
                    ok2, d = self.k.call("DELETE", f"/gtt/triggers/{g['id']}", tries=2); dirty = True
                    log(f"sweep: stop-loss GTT {g['id']} of {(g.get('condition') or {}).get('tradingsymbol')} -> delete " + ("OK" if ok2 else f"failed: {d}"))
        for sym, legs in pos.items():                                  # 4. open positions
            if not any(self.in_scope(l) for l in legs) or not self.claim(sym): continue
            dirty = True
            try:
                log(f"sweep: OPEN POSITION {sym} {sum(l['qty'] for l in legs):+d} found after {EXIT_AT} -> closing")
                self.flatten(sym, sum(l["qty"] for l in legs), "SHPSWEEP")
            finally:
                self.release(sym)
        return not dirty

    def run(self):
        """Returns True when the job ended clean."""
        last_poll = last_sweep = 0.0; clean = 0
        while not self.stop:
            now = now_ist(); t = now.time()
            if t >= JOB_END: break
            if not self.holiday_checked and t >= HOLIDAY_CHECK:
                self.holiday_checked = True
                if not self.market_open_today():
                    log("No trading today (NIFTY has no trade yet) - leaving alerts untouched and stopping"); return True
            if not self.entry_closed and t >= ENTRY_END:
                self.entry_closed = True; threading.Thread(target=self.close_entries, daemon=True).start()
            self.due_exits()
            if time.time() - last_poll >= POLL_S and t >= dtime(9, 14, 30):
                last_poll = time.time(); self.poll()
            if t >= EXIT_AT and time.time() - last_sweep >= SWEEP_GAP_S:
                last_sweep = time.time()
                clean = clean + 1 if self.sweep() else 0
                if clean >= 2: log(f"SWEEP CLEAN at {now_ist():%H:%M:%S}: no waiting alert, no waiting order, no stop-loss GTT, no open position"); break
            near = any((not tr["closed"]) and 0 <= (datetime.combine(now.date(), tr["exit_at"]) - now).total_seconds() < 2 for tr in list(self.trades.values()))
            sleep(0.005 if near else 0.1)
        ok = clean >= 2
        if not ok:                                                      # job end reached with something left: one last try, then fail loudly
            ok = self.sweep() and self.sweep()
            if not ok: log("!!! NOT CLEAN at the end of the job - check Kite for open positions, waiting orders and SHP| alerts NOW")
        log(f"done. entries seen: {len(self.trades)}")
        return ok


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
        log(f"It is {t:%H:%M:%S}. The manager only works between 08:00 and {JOB_END} IST (entries end {ENTRY_END}, square-off {EXIT_AT}, "
            f"clean-up sweep until it is clean). Outside that window it does nothing. To delete waiting alerts right now use mode=cleanup.")
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
    if not m.run(): sys.exit(1)                                      # not clean at the end -> the GitHub run fails and mails you


if __name__ == "__main__":
    main()
