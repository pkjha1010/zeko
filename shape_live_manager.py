"""
SHAPELIVE MANAGER  -  runs on every trading day, from about 09:00 IST until the last strategy is squared off (about 15:30).  It is started by cron-job.org
(see CRON_JOB_ORG.md); because one GitHub job may not run more than 6 hours it is started TWICE (09:00 -> 12:58 and 12:55 -> 15:45).  It keeps no state
of its own: the second run rebuilds everything from Kite (today's orders, positions, alerts) and the plan file.

Three strategies (shape_live_plan.py makes one ATO alert per setup; Zerodha does the ENTRY on its servers: BUY option NRML, validity DAY, with the built-in
GTT: target +60 % for WRL and BPB, +400 % for WRU, stop-loss -20 % for WRU only):
    WRU WICK_RED_UPPER  SHORT  entries to 09:30:00, square off 09:30:00        WRL WICK_RED_LOWER  LONG   entries to 10:00:00, square off 10:00:00
    BPB BREAKOUT_BPB    SHORT  entries to 15:21:00, square off 15:30:00        (the times come from the plan file; env EXIT_AT / ENTRY_END do not exist any more)

SAFETY RULES (all checked every POLL_S = 0.3 s)
  1. A BUY order that is still unfilled ENTRY_WAIT_S (0.5 s) after we see it is CANCELLED at once (price ran away / market order turned into a waiting limit order).
     A partly filled BUY: the rest is cancelled and the filled quantity is managed as a trade.
  2. A stop-loss SELL (the GTT sells with a LIMIT order; price below the entry price) that is still waiting STOP_WAIT_S (0.5 s) after we see it is CANCELLED and the
     position is SQUARED OFF at once (MARKET, then more and more aggressive LIMIT orders until flat).
     A target SELL (price above the entry price) that is still waiting TP_WAIT_S (3 s; -1 = never) is treated the same: cancelled and the position sold at market,
     so that a 60 % winner is not lost again.
  3. FUND LIMIT: if the account uses MORE than FUND_LIMIT_PCT (30) % of the total funds, every waiting SHP| alert is deleted and every waiting BUY order is cancelled
     (no new order can be placed).  When the used funds fall back under the limit the alerts are RE-CREATED from the plan (only strategies still inside their entry time,
     only setups that have not traded yet, and only while used funds + that trade's cost stay within the limit).
     used = Kite utilised.debits (EVERYTHING in the account, also your swing positions);  total = TOTAL_FUNDS env, else Kite net + utilised.debits.
  4. ENTRY END of a strategy: its waiting alerts are deleted, its waiting BUY orders cancelled.  A BUY placed after that second which still fills is sold at once.
  5. EXIT of a strategy (its exit time, to the second): its GTTs are deleted, its waiting orders cancelled, every position of its options squared off.  Then a sweep repeats
     every second until two passes in a row find nothing left (alerts, waiting orders, GTTs, positions of that strategy's options).
  6. At the end (FINAL_END 15:45) anything left makes the job FAIL, so you get the GitHub failure mail.  Handover jobs (JOB_END before FINAL_END) end quietly.
Scope: only the options of today's plan and orders tagged SHP* are touched (SWG alerts, swing positions and other orders are left alone).  SWEEP_ALL=1 additionally
clears EVERY open NFO position and waiting NFO order of the account after the last strategy has finished (default 0 - you run a swing strategy in the same account).
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
    h, m, *sec = [int(x) for x in str(s).split(":")]
    return dtime(h, m, sec[0] if sec else 0)


DEFAULT_STRATS = {   # used only when an old plan file has no "strategies" block
    "WRU": dict(name="WICK_RED_UPPER", entry_end="09:30:00", exit_at="09:30:00", sl_pct=20.0, tp_pct=400.0),
    "WRL": dict(name="WICK_RED_LOWER", entry_end="10:00:00", exit_at="10:00:00", sl_pct=0.0, tp_pct=60.0),
    "BPB": dict(name="BREAKOUT_BPB", entry_end="15:21:00", exit_at="15:30:00", sl_pct=0.0, tp_pct=60.0)}
FINAL_END = hhmmss(os.environ.get("FINAL_END") or "15:45:00")       # all strategies must be clean by now, otherwise the job fails
JOB_END = hhmmss(os.environ.get("JOB_END") or str(FINAL_END))       # this job stops here; if before FINAL_END it is a quiet hand-over to the next job
JOB_START = hhmmss(os.environ.get("JOB_START") or "08:00:00")        # the manager does nothing before this
HOLIDAY_CHECK = hhmmss("09:20:00")
POLL_S = float(os.environ.get("POLL_S") or 0.3)
FUNDS_POLL_S = float(os.environ.get("FUNDS_POLL_S") or 2.0)
SWEEP_GAP_S = float(os.environ.get("SWEEP_GAP_S") or 1.0)           # pause between two sweep passes
SWEEP_ALL = (os.environ.get("SWEEP_ALL") or "0") == "1"             # 1 = after the last strategy also sweep EVERY NFO position / waiting NFO order of the account
STOP_WAIT_S = float(os.environ.get("STOP_WAIT_S") or 0.5)           # a stop-loss SELL still unfilled after this many seconds is cancelled and sold at market
TP_WAIT_S = float(os.environ.get("TP_WAIT_S") or 3)                 # a target SELL still unfilled after this many seconds is cancelled and sold at market (-1 = leave it)
ENTRY_WAIT_S = float(os.environ.get("ENTRY_WAIT_S") or 0.5)         # a BUY order still unfilled after this many seconds is cancelled
FUND_LIMIT_PCT = float(os.environ.get("FUND_LIMIT_PCT") or 30)      # stop new orders while the account uses more than this % of the total funds
TOTAL_FUNDS = float(os.environ.get("TOTAL_FUNDS") or 0)             # total funds in Rs (0 = Kite net + utilised debits)
REARM = (os.environ.get("REARM") or "1") == "1"                     # re-create the waiting alerts when the funds are back under the limit
ORDER_PROXY = (os.environ.get("ORDER_PROXY") or "").strip()         # static-IP proxy, e.g. http://user:pass@host:port  (orders only)
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
    WAIT_SELL_STATES = tuple(x for x in OPEN_STATES if x != "TRIGGER PENDING")      # a trigger-pending order is waiting for its trigger on purpose

    def __init__(self, k, plan):
        self.k, self.lock = k, threading.RLock()
        self.strats = {c: dict(DEFAULT_STRATS.get(c, {}), **r) for c, r in (plan.get("strategies") or {}).items()} or dict(DEFAULT_STRATS)
        for c, r in self.strats.items(): r["_entry_end"], r["_exit_at"] = hhmmss(r["entry_end"]), hhmmss(r["exit_at"])
        self.items = {}
        for i in plan["items"]:                         # one option = one strategy (the plan guarantees it)
            if i["option"] in self.items: continue
            self.items[i["option"]] = dict(i, strategy=i.get("strategy") or "BPB")
        self.seen, self.trades = set(), {}              # order ids handled; option -> trade dict
        self.bought = set()                             # options whose entry order was seen (the alert has fired - it must not be re-created)
        self.armed = {s for s, i in self.items.items() if i.get("alert")}     # options whose alert is believed to be waiting (corrected from Kite in bootstrap)
        self.entry_done, self.clean, self.holiday_checked, self.stop = set(), {}, False, False
        self.open_seen, self.cancel_tries, self.stop_handled = {}, {}, set()
        self.busy = set()
        self.blocked, self.used, self.total, self.limit, self.last_funds = False, 0.0, 0.0, 0.0, 0.0
        self.final = False                              # True after the last strategy is finished (only then SWEEP_ALL applies)

    # ------------------------------------------------------------ small helpers
    def cfg(self, sym): return self.strats.get((self.items.get(sym) or {}).get("strategy") or "", {})
    def strategy_of(self, sym): return (self.items.get(sym) or {}).get("strategy")
    def entry_end(self, sym): return self.cfg(sym).get("_entry_end", dtime(9, 30)) if sym in self.items else dtime(23, 59)
    def exit_at(self, sym): return self.cfg(sym).get("_exit_at", dtime(15, 30))

    def in_scope(self, row):
        """An order / position row of today's plan options or tagged SHP*.  After the last strategy: SWEEP_ALL=1 -> every NFO row."""
        if SWEEP_ALL and self.final and row.get("exchange") == "NFO": return True
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

    # ------------------------------------------------------------ a BUY fill is noticed
    def on_order(self, o):
        try:
            sym = o.get("tradingsymbol")
            if sym not in self.items or o.get("transaction_type") != "BUY": return
            self.bought.add(sym); self.armed.discard(sym)
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
        late = ft.time() >= self.entry_end(sym)
        with self.lock:
            tr = self.trades.get(sym)
            if tr and not tr["closed"]:                 # a second fill of the same option (partial fills): average in
                tot = tr["qty"] + qty; tr["entry"] = (tr["entry"] * tr["qty"] + px * qty) / tot; tr["qty"] = tot
                log(f"{sym}: another fill {qty} @ {px} -> total {tot} @ {tr['entry']:.2f}")
                return
            tr = dict(sym=sym, qty=qty, entry=px, fill=ft, closed=False, strat=self.strategy_of(sym), exit_at=self.exit_at(sym))
            self.trades[sym] = tr
        c = self.cfg(sym)
        if late:
            log(f"{sym}: [{tr['strat']}] entry placed {ft:%H:%M:%S} is after {c.get('entry_end')} -> selling at once")
            self.close_now(tr, "SHPLATE"); return
        log(f"{sym}: [{tr['strat']}] ENTRY {qty} @ {px} placed {ft:%H:%M:%S} -> target {px * (1 + c.get('tp_pct', 60) / 100):.2f}"
            + (f", stop-loss {px * (1 - c.get('sl_pct', 0) / 100):.2f}" if c.get("sl_pct") else ", no stop-loss") + f", square off {tr['exit_at']}")

    # ------------------------------------------------------------ positions, GTT, waiting orders
    def positions(self):
        """{option: [open legs]} of every non-zero net position, or None if Kite could not be read."""
        ok, d = self.k.call("GET", "/portfolio/positions", tries=3)
        if not ok or not isinstance(d, dict): return None
        out = {}
        for p in d.get("net", []) or []:
            q = int(p.get("quantity") or 0)
            if q: out.setdefault(p["tradingsymbol"], []).append(dict(tradingsymbol=p["tradingsymbol"], qty=q, product=p.get("product") or PRODUCT, exchange=p.get("exchange") or "NFO",
                                                                      average_price=float(p.get("average_price") or 0)))
        return out

    def drop_gtt(self, sym):
        """Delete the ATO's built-in GTT (target / stop-loss) for this option so it cannot trigger after we square off."""
        if DRY_RUN: log(f"DRY delete active GTTs of {sym}"); return 0
        ok, rows = self.k.call("GET", "/gtt/triggers", tries=2)
        if not ok: log(f"{sym}: cannot list GTTs: {rows}"); return 0
        n = 0
        for g in rows or []:
            if g.get("status") == "active" and (g.get("condition") or {}).get("tradingsymbol") == sym:
                ok2, d = self.k.call("DELETE", f"/gtt/triggers/{g['id']}", tries=2); n += ok2
                if not ok2: log(f"{sym}: GTT {g['id']} delete failed: {d}")
        if n: log(f"{sym}: GTT removed: {n}")
        return n

    def cancel_waiting(self, sym=None, side=None, syms=None):
        """Cancel waiting orders.  sym: every waiting order of that option (a waiting stop-loss limit order RESERVES the quantity, so a new sell is refused until it is cancelled).
        syms: every waiting order of those options.  none given: every waiting order in scope.  side limits it to BUY or SELL."""
        ok, rows = self.k.call("GET", "/orders", tries=2)
        if not ok: log(f"cannot read the order book: {rows}"); return 0
        n = 0
        for o in rows or []:
            if str(o.get("status", "")).upper() not in self.OPEN_STATES: continue
            if sym and o.get("tradingsymbol") != sym: continue
            if syms is not None and o.get("tradingsymbol") not in syms: continue
            if side and o.get("transaction_type") != side: continue
            if not sym and syms is None and not self.in_scope(o): continue
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
        From attempt 3: a LIMIT priced THROUGH the best bid (SELL) / ask (BUY) by 3 % x (attempt - 2), so it fills even when the market order keeps turning into a waiting limit order."""
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
            if not legs: log(f"{sym}: nothing open" + (" (already closed)" if attempt == 1 else " - flat")); return True
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
        if not self.claim(tr["sym"]): return                           # a sweep is already closing this option
        try:
            t0 = time.time()
            self.drop_gtt(tr["sym"])
            self.flatten(tr["sym"], tr["qty"], tag)
            log(f"{tr['sym']}: [{tr.get('strat')}] exit sequence ({tag}) done in {(time.time() - t0) * 1000:.0f} ms")
        finally:
            self.release(tr["sym"])

    def close_untracked(self, sym, tag):
        """A position / waiting stop-loss of a plan option whose BUY we never noticed: same exit sequence."""
        if not self.claim(sym): return
        try:
            self.drop_gtt(sym); self.flatten(sym, 0, tag)
        finally:
            self.release(sym)

    def due_exits(self):
        t = now_ist().time()
        with self.lock:
            due = [tr for tr in self.trades.values() if not tr["closed"] and t >= tr["exit_at"]]
        for tr in due: threading.Thread(target=self.close_now, args=(tr, "SHPEXIT"), daemon=True).start()

    # ------------------------------------------------------------ alerts
    def my_alerts(self):
        ok, rows = self.k.call("GET", "/alerts", tries=3, params={"status": "enabled", "page_size": 100})
        if not ok: return None
        return [a for a in (rows or []) if str(a.get("name", "")).startswith(PFX) and a.get("status") == "enabled"]

    def delete_alerts(self, strat=None):
        """Delete every waiting SHP| alert (of one strategy when strat is given: SHP|WRL|...).  Returns how many were found."""
        rows = self.my_alerts()
        pre = f"{PFX}{strat}|" if strat else PFX
        ids = [a["uuid"] for a in (rows or []) if str(a.get("name", "")).startswith(pre)]
        names = {a["uuid"]: a["name"] for a in (rows or [])}
        if DRY_RUN: log(f"DRY delete {len(ids)} waiting alerts ({strat or 'all'})"); return 0
        for i in range(0, len(ids), 50): self.k.call("DELETE", "/alerts", tries=3, params=[("uuid", u) for u in ids[i:i + 50]])
        if ids:
            gone = {n for n in (names[u] for u in ids)}
            with self.lock: self.armed -= {s for s, it in self.items.items() if it.get("alert") in gone}
            log(f"deleted {len(ids)} waiting {PFX}{strat or ''} alerts")
        return len(ids)

    def arm(self, sym):
        it = self.items[sym]
        if not it.get("alert_data"): return False
        if DRY_RUN: log(f"DRY re-create alert {it['alert']}"); return True
        ok, d = self.k.call("POST", "/alerts", tries=2, data=it["alert_data"])
        log(f"re-created alert {it['alert']}: " + ("OK" if ok else f"FAILED {d}"))
        if ok: self.armed.add(sym)
        return ok

    def strategy_syms(self, code): return {s for s, i in self.items.items() if i["strategy"] == code}

    def close_entries(self, code):
        """ENTRY END of a strategy: its waiting alerts deleted, its waiting BUY orders cancelled."""
        self.delete_alerts(code)
        n = self.cancel_waiting(side="BUY", syms=self.strategy_syms(code))
        self.armed -= self.strategy_syms(code)
        log(f"[{code}] entry time over ({self.strats[code]['entry_end']}): alerts deleted, waiting BUY orders cancelled: {n}")

    # ------------------------------------------------------------ fund limit
    def read_funds(self):
        ok, d = self.k.call("GET", "/user/margins/equity", tries=2)
        if not ok or not isinstance(d, dict): return None
        used = float((d.get("utilised") or {}).get("debits") or 0)
        net = float(d.get("net") or (d.get("available") or {}).get("live_balance") or 0)
        total = TOTAL_FUNDS if TOTAL_FUNDS > 0 else net + used
        return used, total

    def check_funds(self):
        """Fund limit: above FUND_LIMIT_PCT % of the total funds no new order may be placed - waiting alerts are deleted, waiting BUYs cancelled; re-armed when funds are free again."""
        r = self.read_funds()
        if r is None: log("funds could not be read - keeping the last state"); return
        used, total = r; limit = total * FUND_LIMIT_PCT / 100
        self.used, self.total, self.limit = used, total, limit
        over = total > 0 and used > limit
        if over and not self.blocked:
            self.blocked = True
            log(f"FUND LIMIT: used Rs {used:,.0f} is above {FUND_LIMIT_PCT:g}% of Rs {total:,.0f} (= Rs {limit:,.0f}) -> NEW ORDERS STOPPED: deleting waiting alerts and cancelling waiting BUY orders")
            self.delete_alerts(); self.cancel_waiting(side="BUY")
        elif not over and self.blocked:
            self.blocked = False; log(f"FUND LIMIT: used Rs {used:,.0f} is back within {FUND_LIMIT_PCT:g}% (Rs {limit:,.0f}) -> new orders allowed again")
        if not over and REARM and total > 0:
            t = now_ist().time()
            for sym, it in sorted(self.items.items(), key=lambda x: x[1]["cost"]):
                if sym in self.bought or sym in self.armed or it["strategy"] in self.entry_done or t >= self.entry_end(sym) or not it.get("alert_data"): continue
                if used + it["cost"] <= limit: self.arm(sym)          # this trade, if it fired now, would still keep the account within the limit

    # ------------------------------------------------------------ market checks
    def market_open_today(self):
        ok, d = self.k.call("GET", "/quote", tries=3, params=[("i", "NSE:NIFTY 50")])
        if not ok: return True                                   # unknown: assume open (safer to keep managing)
        lt = as_dt(d["NSE:NIFTY 50"].get("last_trade_time"))
        return bool(lt and lt.date() == now_ist().date())

    def watch_open(self, o):
        """A BUY order that is still unfilled ENTRY_WAIT_S after we first see it is cancelled - at once after the strategy's entry end or while the fund limit blocks."""
        if o.get("transaction_type") != "BUY" or str(o.get("status", "")).upper() not in self.OPEN_STATES or not self.in_scope(o): return
        oid = str(o.get("order_id")); sym = o.get("tradingsymbol")
        first = self.open_seen.setdefault(oid, time.time())
        urgent = now_ist().time() >= self.entry_end(sym) or self.blocked
        if (not urgent and time.time() - first < ENTRY_WAIT_S) or self.cancel_tries.get(oid, 0) >= 8: return
        self.cancel_tries[oid] = self.cancel_tries.get(oid, 0) + 1
        if DRY_RUN: log(f"DRY cancel unfilled BUY {sym} {oid}"); self.cancel_tries[oid] = 99; return
        ok, d = self.k.call("DELETE", f"/orders/{o.get('variety') or 'regular'}/{oid}", tries=1)
        log(f"{sym}: BUY {oid} still unfilled" + (" (fund limit)" if self.blocked else "") + " -> cancel " + ("OK" if ok else f"not done ({d}) - may have filled just now"))

    def watch_sell(self, o):
        """The GTT sells with a LIMIT order.  Below the entry price it is the STOP-LOSS: if it waits, price ran past it -> cancel and sell at market.
        Above the entry price it is the TARGET: if it waits more than TP_WAIT_S, cancel and sell at market (lock the profit).  Our own SHP* close orders are handled inside flatten()."""
        sym = o.get("tradingsymbol")
        if o.get("transaction_type") != "SELL" or str(o.get("status", "")).upper() not in self.WAIT_SELL_STATES or not self.in_scope(o): return
        if str(o.get("tag") or "").startswith("SHP") and str(o.get("tag")) in ("SHPEXIT", "SHPSTOP", "SHPTP", "SHPLATE", "SHPSWEEP"): return
        tr = self.trades.get(sym)
        if tr and tr["closed"]: return
        price = float(o.get("price") or 0)
        kind = "TP" if (tr and price > 0 and price >= tr["entry"]) else "SL"
        wait = STOP_WAIT_S if kind == "SL" else TP_WAIT_S
        if wait < 0: return
        oid = str(o.get("order_id")); first = self.open_seen.setdefault(oid, time.time())
        if time.time() - first < wait or oid in self.stop_handled: return
        self.stop_handled.add(oid)
        what = "stop-loss" if kind == "SL" else "target"
        log(f"{sym}: {what} SELL {oid} still waiting after {wait}s -> cancel it and sell the position at market")
        tag = "SHPSTOP" if kind == "SL" else "SHPTP"
        if tr: threading.Thread(target=self.close_now, args=(tr, tag), daemon=True).start()
        else: threading.Thread(target=self.close_untracked, args=(sym, tag), daemon=True).start()

    def poll(self):
        ok, rows = self.k.call("GET", "/orders", tries=1)
        if ok:
            for o in rows or []:
                self.on_order(o); self.watch_open(o); self.watch_sell(o)

    # ------------------------------------------------------------ restart: rebuild the state from Kite
    def bootstrap(self):
        ok, rows = self.k.call("GET", "/orders", tries=3)
        for o in (rows or []) if ok else []:
            if o.get("transaction_type") == "BUY" and o.get("tradingsymbol") in self.items and str(o.get("order_timestamp", ""))[:10] == str(now_ist().date()): self.on_order(o)
        pos = self.positions() or {}
        for sym, legs in pos.items():                                 # an open position of a plan option whose BUY order we did not see
            if sym in self.items and sym not in self.trades and sum(l["qty"] for l in legs) > 0:
                q = sum(l["qty"] for l in legs); px = legs[0].get("average_price") or float(self.items[sym]["close_price"])
                self.trades[sym] = dict(sym=sym, qty=q, entry=px, fill=now_ist(), closed=False, strat=self.strategy_of(sym), exit_at=self.exit_at(sym)); self.bought.add(sym)
                log(f"{sym}: open position {q} @ {px:.2f} found at start -> managed by [{self.strategy_of(sym)}]")
        rows = self.my_alerts()
        if rows is not None:
            names = {a["name"] for a in rows}
            self.armed = {s for s, i in self.items.items() if i.get("alert") in names}
        t = now_ist().time()
        for c in self.strats:                                         # strategies whose entry time is over already
            if t >= self.strats[c]["_entry_end"]: self.entry_done.add(c)
        log(f"start: {len(self.items)} planned options, {len(self.trades)} open trades, {len(self.armed)} waiting alerts; strategies: "
            + ", ".join(f"{c} entries to {r['entry_end']} exit {r['exit_at']}" for c, r in self.strats.items()))

    # ------------------------------------------------------------ the per-strategy sweep after its exit time
    def sweep(self, code=None):
        """One clean-up pass for one strategy (or, after the last one with SWEEP_ALL=1, the whole account).  True = nothing was left."""
        if self.busy: return False                                      # an exit is still running - judge again next pass
        dirty = False
        syms = self.strategy_syms(code) if code else set(self.items)
        if code:
            if self.delete_alerts(code): dirty = True                  # 1. pending ATO alerts of the strategy
            if self.cancel_waiting(syms=syms): dirty = True            # 2. waiting orders of its options, BUY and SELL
        else:
            if self.delete_alerts(): dirty = True
            if self.cancel_waiting(): dirty = True
        pos = self.positions()
        if pos is None: log("sweep: positions could not be read - will check again"); return False
        if not code: syms = syms | {s for s, legs in pos.items() if any(self.in_scope(l) for l in legs)}
        if not DRY_RUN:                                                # 3. GTTs of those options
            ok, rows = self.k.call("GET", "/gtt/triggers", tries=2)
            for g in (rows or []) if ok else []:
                if g.get("status") == "active" and (g.get("condition") or {}).get("tradingsymbol") in syms:
                    ok2, d = self.k.call("DELETE", f"/gtt/triggers/{g['id']}", tries=2); dirty = True
                    log(f"sweep: GTT {g['id']} of {(g.get('condition') or {}).get('tradingsymbol')} -> delete " + ("OK" if ok2 else f"failed: {d}"))
        for sym, legs in pos.items():                                  # 4. open positions
            if sym not in syms or not self.claim(sym): continue
            dirty = True
            try:
                log(f"sweep: OPEN POSITION {sym} {sum(l['qty'] for l in legs):+d} found after the exit time -> closing")
                self.flatten(sym, sum(l["qty"] for l in legs), "SHPSWEEP")
            finally:
                self.release(sym)
        return not dirty

    def run(self):
        """Returns True when the job ended clean (or handed over)."""
        self.bootstrap(); self.check_funds()
        last_poll = last_funds = last_sweep = 0.0
        while not self.stop:
            now = now_ist(); t = now.time()
            if t >= JOB_END: break
            if not self.holiday_checked and t >= HOLIDAY_CHECK:
                self.holiday_checked = True
                if not self.market_open_today():
                    log("No trading today (NIFTY has no trade yet) - leaving alerts untouched and stopping"); return True
            for c, r in self.strats.items():                                   # entry end of each strategy
                if c not in self.entry_done and t >= r["_entry_end"]:
                    self.entry_done.add(c); threading.Thread(target=self.close_entries, args=(c,), daemon=True).start()
            self.due_exits()
            if time.time() - last_poll >= POLL_S and t >= dtime(9, 14, 30):
                last_poll = time.time(); self.poll()
            if time.time() - last_funds >= FUNDS_POLL_S and dtime(8, 55) <= t < FINAL_END:
                last_funds = time.time(); self.check_funds()
            if time.time() - last_sweep >= SWEEP_GAP_S:                         # sweep each strategy after its own exit time
                last_sweep = time.time()
                for c, r in self.strats.items():
                    if t >= r["_exit_at"] and self.clean.get(c, 0) < 2:
                        self.clean[c] = self.clean.get(c, 0) + 1 if self.sweep(c) else 0
                        if self.clean[c] >= 2: log(f"[{c}] SWEEP CLEAN at {now_ist():%H:%M:%S}: no waiting alert, order, GTT or open position of this strategy")
                if all(self.clean.get(c, 0) >= 2 for c in self.strats):
                    if SWEEP_ALL and not self.final:
                        self.final = True; log("all strategies finished - SWEEP_ALL: clearing every NFO position and waiting NFO order of the account")
                    if not SWEEP_ALL or self.sweep(): break
            near = any((not tr["closed"]) and 0 <= (datetime.combine(now.date(), tr["exit_at"]) - now).total_seconds() < 2 for tr in list(self.trades.values()))
            sleep(0.005 if near else 0.1)
        if JOB_END < FINAL_END and now_ist().time() >= JOB_END:
            log(f"hand-over at {JOB_END}: next job continues (open trades {sum(1 for t in self.trades.values() if not t['closed'])}, waiting alerts {len(self.armed)})"); return True
        ok = all(self.clean.get(c, 0) >= 2 for c in self.strats)
        if not ok:                                                      # job end reached with something left: one last try, then fail loudly
            ok = all(self.sweep(c) and self.sweep(c) for c in self.strats)
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
    if t >= FINAL_END or t < JOB_START or t >= JOB_END:
        log(f"It is {t:%H:%M:%S}. The manager only works between {JOB_START} and {JOB_END} IST. Outside that window it does nothing. To delete waiting alerts right now use mode=cleanup.")
        return
    if not os.path.exists(PLAN): sys.exit(f"No plan file {PLAN}")
    plan = json.load(open(PLAN))
    today = str(now_ist().date())
    if plan.get("trade_day") and plan["trade_day"] < today:
        sys.exit(f"Plan is for {plan['trade_day']} (older than today {today}) - the morning planner did not run. Nothing to manage.")
    if not plan["items"]: log("plan has no orders today - nothing to manage"); return
    log(f"plan {plan['trade_day']}: " + ", ".join(f"{i.get('strategy', '')} {i['stock']} {i['direction']} {i['qty']}x{i['option']}" for i in plan["items"]))
    kite, key, tok = login(); k = K(key, tok)
    m = Manager(k, plan)
    if os.environ.get("USE_WS") == "1": start_ticker(m, key, tok)   # free plan: the order WebSocket answers 403, polling is used instead
    if not m.run(): sys.exit(1)                                      # not clean at the end -> the GitHub run fails and mails you


if __name__ == "__main__":
    main()
