"""
Offline test of gap_live_plan.py + gap_live_manager.py against a SIMULATED Zerodha / Angel One (no login, no orders, no internet; a few seconds).
    python gap_live_offline_test.py
Checks
  1. the GAP setup finder (gap_armed) equals an independent day-by-day reference loop on thousands of random price paths (LONG and SHORT), setups are used up on the first touch
  2. plan step: GAPL alerts at once (before the open, no untouched check), GAPS alerts only for setups untouched since the open; nearest-strike option (CE / PE), 7 % sizing, SL 60 / TP 40 GTT,
     Rs 1 premium floor, only the alerts of the planned strategy are replaced, dry run, holiday, evening preview (Fri -> Mon), merge of the two plan steps
  3. manager: LONG cap 4 (5th fill sold SHPCAP, alerts deleted while full, re-armed when a slot frees), entry end 09:30 (late fill sold SHPLATE), hand-over and a fresh afternoon job, 15:30:00 square off
  4. manager: SHORT cap 3, 4th allowed only when 2 of the 3 open shorts are in profit / within -5 %, never a 5th
  5. 30 % fund limit (alerts deleted, waiting BUYs cancelled, a fill that crosses it is sold SHPFUND) and the loss stop (no new trades, open trades keep their exits, later fills sold SHPLOSS)
  6. plan file re-read while the manager runs (GAPS items added at 09:30), position recovery from order tags, stale plan, holiday, stuck market order, DRY_RUN
"""
import os, sys, types, threading
import numpy as np
from datetime import datetime, timedelta, time as dtime
for _m in ("kiteconnect", "pyotp", "requests"):            # only needed for the real login / REST; stubbed when not installed
    try: __import__(_m)
    except ImportError:
        mod = types.ModuleType(_m); sys.modules[_m] = mod
        if _m == "kiteconnect": mod.KiteConnect = object
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gap_live_manager as M
import gap_live_plan as P

DAY = datetime(2026, 10, 19)                               # a Monday


def at(hh, mm, ss=0): return datetime(2026, 10, 19, hh, mm, ss)


class Clock:
    def __init__(self, h, m, s=0): self.t = at(h, m, s); self.hooks = []
    def now(self): return self.t
    def time(self): return (self.t - DAY).total_seconds() + 1_000_000.0
    def sleep(self, s):
        self.t += timedelta(seconds=s)
        for h in self.hooks: h()


class SyncThread:                                          # background threads run inline so the fake clock stays deterministic
    def __init__(self, target=None, args=(), daemon=None): self.target, self.args = target, args
    def start(self): self.target(*self.args)


class Fake:
    OPEN = ("OPEN", "TRIGGER PENDING")

    def __init__(self, clock):
        self.c, self.orders, self.pos, self.gtts, self.alerts, self.events, self.audit = clock, [], {}, [], [], [], []
        self.bid, self.ask, self.market_stuck, self.n, self.mutations, self.an = {}, {}, set(), 0, 0, 0
        self.net, self.debits, self.nifty_day, self.pnl = 700000.0, 0.0, "2026-10-19", {}
        clock.hooks.append(self.fire)

    def at(self, hh, mm, ss, fn): self.events.append([at(hh, mm, ss), fn, False])
    def fire(self):
        for ev in self.events:
            if not ev[2] and ev[0] <= self.c.t: ev[2] = True; ev[1]()
    def stamp(self): return self.c.t.strftime("%Y-%m-%d %H:%M:%S")

    def add_alert(self, name): self.an += 1; self.alerts.append(dict(uuid=f"u{self.an}", name=name, status="enabled")); return self.alerts[-1]
    def alert_names(self): return sorted(a["name"] for a in self.alerts if a["status"] == "enabled")

    def add_order(self, sym, side, status, otype="MARKET", price=0, qty=100, avg=0, ts=None, tag="", product="NRML", exchange="NFO"):
        self.n += 1; o = dict(order_id=str(1000 + self.n), tradingsymbol=sym, transaction_type=side, status=status, order_type=otype, price=price, quantity=qty,
                              filled_quantity=qty if status == "COMPLETE" else 0, average_price=avg, product=product, exchange=exchange, variety="regular", tag=tag,
                              order_timestamp=ts or self.stamp(), placed_at=self.c.t)
        self.orders.append(o)
        if status == "COMPLETE": self.move(sym, product, qty if side == "BUY" else -qty)
        return o

    def fill_order(self, o, px):
        o["status"], o["filled_quantity"], o["average_price"] = "COMPLETE", o["quantity"], px
        self.move(o["tradingsymbol"], o["product"], o["quantity"] if o["transaction_type"] == "BUY" else -o["quantity"]); self.audit.append((self.c.t, "fill", o))

    def avg(self, sym):
        b = [o for o in self.orders if o["tradingsymbol"] == sym and o["transaction_type"] == "BUY" and o["status"] == "COMPLETE" and float(o.get("average_price") or 0) > 0]
        return sum(o["average_price"] * o["filled_quantity"] for o in b) / sum(o["filled_quantity"] for o in b) if b else 50.0
    def move(self, sym, product, dq): self.pos[(sym, product)] = self.pos.get((sym, product), 0) + dq
    def qty(self, sym): return sum(q for (s, _), q in self.pos.items() if s == sym)
    def open_orders(self, sym=None, side=None): return [o for o in self.orders if o["status"] in self.OPEN and (not sym or o["tradingsymbol"] == sym) and (not side or o["transaction_type"] == side)]

    # ------------------------------------------------------------ the REST surface the manager uses
    def call(self, method, path, tries=1, **kw):
        self.fire()
        if method != "GET": self.mutations += 1
        if path == "/orders" and method == "GET": return True, [dict(o) for o in self.orders]
        if path == "/orders/regular" and method == "POST": return self.place(kw["data"])
        if path.startswith("/orders/") and method == "PUT":                       # modify a waiting order (price / order type)
            oid = path.rsplit("/", 1)[1]; o = next((x for x in self.orders if x["order_id"] == oid), None)
            if not o or o["status"] not in self.OPEN: return False, "order cannot be modified"
            o["order_type"], o["price"] = kw["data"].get("order_type", o["order_type"]), float(kw["data"].get("price") or o["price"]); self.audit.append((self.c.t, "modify", dict(o)))
            sym, side = o["tradingsymbol"], o["transaction_type"]; bid, ask = self.bid.get(sym, 50), self.ask.get(sym, 51)
            fill = (ask if o["price"] >= ask else None) if side == "BUY" else (bid if o["price"] <= bid else None)       # a limit order fills at the market price
            if fill is not None: self.fill_order(o, fill)
            return True, {"order_id": oid}
        if path.startswith("/orders/") and method == "DELETE":
            oid = path.rsplit("/", 1)[1]; o = next((x for x in self.orders if x["order_id"] == oid), None)
            if o and o["status"] in self.OPEN: o["status"] = "CANCELLED"; self.audit.append((self.c.t, "cancel", o)); return True, {"order_id": oid}
            return False, "order cannot be cancelled"
        if path == "/portfolio/positions": return True, {"net": [dict(tradingsymbol=s, product=p, exchange="NFO", quantity=q, average_price=self.avg(s), pnl=self.pnl.get(s, 0.0)) for (s, p), q in self.pos.items() if q], "day": []}
        if path == "/user/margins/equity": return True, {"net": self.net, "available": {"live_balance": self.net}, "utilised": {"debits": self.debits}}
        if path == "/gtt/triggers" and method == "GET": return True, [dict(g) for g in self.gtts]
        if path == "/gtt/triggers" and method == "POST":
            import json as _jj
            g = dict(id=9000 + len(self.gtts), status="active", condition=_jj.loads(kw["data"]["condition"]), orders=_jj.loads(kw["data"]["orders"])); self.gtts.append(g); self.audit.append((self.c.t, "gtt+", g))
            return True, {"trigger_id": g["id"]}
        if path.startswith("/gtt/triggers/") and method == "DELETE":
            for g in self.gtts:
                if str(g["id"]) == path.rsplit("/", 1)[1]: g["status"] = "deleted"
            return True, {}
        if path == "/alerts" and method == "GET": return True, [dict(a) for a in self.alerts if a["status"] == "enabled"]
        if path == "/alerts" and method == "POST": a = self.add_alert(kw["data"]["name"]); self.audit.append((self.c.t, "alert+", a)); return True, {"uuid": a["uuid"]}
        if path == "/alerts" and method == "DELETE":
            ids = {u for k, u in kw["params"] if k == "uuid"}
            for a in self.alerts:
                if a["uuid"] in ids and a["status"] == "enabled": a["status"] = "deleted"; self.audit.append((self.c.t, "alert-", a))
            return True, {}
        if path == "/quote":
            sym = kw["params"][0][1]
            if sym == "NSE:NIFTY 50": return True, {sym: {"last_trade_time": f"{self.nifty_day} 09:20:00"}}
            b = sym.split(":")[1]; return True, {sym: {"last_price": self.bid.get(b, 50), "depth": {"buy": [{"price": self.bid.get(b, 50)}], "sell": [{"price": self.ask.get(b, 51)}]}}}
        return False, f"unhandled {method} {path}"

    def place(self, d):
        sym, side, qty, prod = d["tradingsymbol"], d["transaction_type"], int(d["quantity"]), d["product"]
        if side == "SELL" and sum(o["quantity"] for o in self.open_orders(sym, "SELL")) + qty > self.qty(sym):
            return False, "Insufficient stock / quantity reserved by another waiting order"      # like Kite: a waiting sell RESERVES the quantity
        o = self.add_order(sym, side, "OPEN", d["order_type"], float(d.get("price") or 0), qty, tag=d.get("tag", ""), product=prod)
        bid, ask = self.bid.get(sym, 50), self.ask.get(sym, 51)
        if d["order_type"] == "MARKET": fill = None if sym in self.market_stuck else (bid if side == "SELL" else ask)
        else: p = float(d["price"]); fill = (p if p <= bid else None) if side == "SELL" else (p if p >= ask else None)
        if fill is not None:
            o["status"], o["filled_quantity"], o["average_price"] = "COMPLETE", qty, fill; self.move(sym, prod, qty if side == "BUY" else -qty); self.audit.append((self.c.t, "fill", o))
        return True, {"order_id": o["order_id"]}

LOGS = []
RULES = {c: dict(r) for c, r in M.DEFAULT_STRATS.items()}
LIQUID_OPT = {"oi": 10_000_000, "volume": 1_000_000, "depth": {"buy": [{"price": 40.0, "quantity": 10_000_000, "orders": 5}], "sell": [{"price": 40.05, "quantity": 10_000_000, "orders": 5}]}}
TODAY = "2026-10-19"


def dump(n=60): return "\n--- last log lines ---\n" + "\n".join(LOGS[-n:])


def need(cond, msg):
    if not cond: raise AssertionError(msg + dump())


def setup(sweep_all=False, dry=False, start=(9, 14), job_end=None, fund=0.0, loss=0.0):
    clock = Clock(*start); fake = Fake(clock)
    M.time, M.now_ist, M.SWEEP_ALL, M.DRY_RUN = clock, clock.now, sweep_all, dry
    M.GTT_REBUILD, M.BUY_CHASE = False, False
    M.FUND_LIMIT_PCT, M.TOTAL_FUNDS, M.LOSS_STOP_PCT, = fund, 0.0, loss
    M.PLAN = "/nonexistent/plan.json"
    M.JOB_END = job_end or M.FINAL_END
    M.threading = types.SimpleNamespace(RLock=threading.RLock, Thread=SyncThread)
    M.log = lambda m: LOGS.append(f"[{clock.now():%H:%M:%S}] {m}")
    return clock, fake


def plan(*rows):
    """rows = (option, strategy, cost, price)"""
    items = [dict(option=o, strategy=s, qty=100, close_price=px, cost=c, stock="K" + o, direction="LONG" if s == "GAPL" else "SHORT", ref=100.0 if s == "GAPL" else 40.0, alert=f"SHP|{s}|{o}|100.00",
                  alert_data=dict(name=f"SHP|{s}|{o}|100.00", type="ato")) for o, s, c, px in rows]
    return {"trade_day": TODAY, "strategies": {c: dict(RULES[c]) for c in {r[1] for r in rows}}, "items": items}


def arm_all(fake, pl):
    for i in pl["items"]: fake.add_alert(i["alert"])


def buy(fake, sym, tag, px=50.0, ts=None, qty=100):
    fake.add_order(sym, "BUY", "COMPLETE", "MARKET", 0, qty, avg=px, ts=ts or fake.stamp(), tag=tag)
    fake.gtts.append(dict(id=len(fake.gtts) + 1, status="active", condition=dict(tradingsymbol=sym)))


def sold(fake):
    out = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": out.setdefault(o["tradingsymbol"], (t, o))
    return out


def tags(fake, sym): return [o.get("tag") for o in fake.orders if o["tradingsymbol"] == sym and o["transaction_type"] == "SELL"]


# ---------------------------------------------------------------------------------------------------------------- 1. setup finder vs a plain reference loop
def ref_setups(O, H, L, C, long, gap=0.01):
    """Independent day-by-day version of gap_setups() of the Dhan repo (original backtest loop), asked: which setups have today (index n) as their first possible entry day?"""
    n = len(C); res = []
    for i in range(1, n - 1):
        g = (O[i] - C[i - 1]) / C[i - 1]
        if long: cond = g > gap and C[i + 1] < C[i]
        else: cond = g < -gap and C[i + 1] > C[i]
        if not cond: continue
        lvl = H[i] if long else L[i]
        hit = None
        for j in range(i + 2, i + 5):
            if j >= n: break
            if (H[j] >= lvl) if long else (L[j] <= lvl): hit = j; break
        if hit is None and i + 2 <= n <= i + 4: res.append((i, float(lvl)))
    return res


def check_setups():
    rng = np.random.default_rng(7); found = {True: 0, False: 0}; paths = 0
    for _ in range(6000):
        n = int(rng.integers(6, 30)); C = [100.0]; O, H, L = [100.0], [100.5], [99.5]
        for _d in range(n - 1):
            gap = float(rng.choice([0, 0, 0, 0.02, -0.02, 0.035, -0.035, 0.012, -0.012])) + float(rng.normal(0, 0.004))
            o = C[-1] * (1 + gap); c = o * (1 + float(rng.normal(0, 0.012))); hi = max(o, c) * (1 + abs(float(rng.normal(0, 0.006)))); lo = min(o, c) * (1 - abs(float(rng.normal(0, 0.006))))
            O.append(o); H.append(hi); L.append(lo); C.append(c)
        O, H, L, C = map(np.array, (O, H, L, C)); paths += 1
        for long in (True, False):
            a = sorted((x["signal"], round(x["level"], 6)) for x in P.gap_armed(O, H, L, C, long)); b = sorted((i, round(l, 6)) for i, l in ref_setups(O, H, L, C, long))
            need(a == b, f"gap_armed != reference ({'LONG' if long else 'SHORT'}): {a} vs {b}")
            found[long] += len(a)
    need(found[True] > 300 and found[False] > 300, f"too few setups produced by the random paths {found}")
    # hand-made case: LONG gap day i=3, next day closes lower, level = H[3]; used up when day i+2 / i+3 touches it
    C = [100, 100, 100, 104, 103, 101, 101.0]; O = [100, 100, 100, 103, 104, 102, 101.0]; H = [100.5] * 3 + [105, 104.5, 103, 101.5]; L = [99.5] * 3 + [102, 102, 100, 100.5]
    r = P.gap_armed(O, H, L, C, True)
    need(len(r) == 1 and r[0]["signal"] == 3 and r[0]["level"] == 105 and r[0]["day"] == 4, f"hand-made LONG: {r}")
    need(P.gap_armed(O[:6], H[:6], L[:6], C[:6], True)[0]["day"] == 3, "entry day counting")
    H2 = list(H); H2[5] = 105.2
    need(P.gap_armed(O, H2, L, C, True) == [], "a level touched on an earlier entry day must use the setup up")
    need(P.gap_armed(O, H, L, C + [100.0], True) == [], "after the 3rd entry day (i+4) the setup is over")
    print(f"PASS setups: gap_armed equals the reference loop on {paths} random paths (LONG {found[True]}, SHORT {found[False]} setups) | used-up / entry-day rules on hand-made data")


# ---------------------------------------------------------------------------------------------------------------- 2. plan step
def check_plan():
    import tempfile, json as _j
    need(P.STRATS["GAPL"]["max_open"] == 4 and P.STRATS["GAPS"]["max_open"] == 3 and P.STRATS["GAPS"]["max_open_extra"] == 4, "caps 4 / 3 (+1)")
    need(P.STRATS["GAPL"]["sl_pct"] == 60 and P.STRATS["GAPL"]["tp_pct"] == 40 and P.STRATS["GAPS"]["sl_pct"] == 60 and P.STRATS["GAPS"]["tp_pct"] == 40, "SL 60 / TP 40")
    need(P.STRATS["GAPL"]["entry_end"] == "09:30:00" and P.STRATS["GAPS"]["entry_end"] == "14:00:00" and P.ALERT_AT == "09:30:00", "entry windows / ALERT_AT")
    need(P.gtt_for(P.STRATS["GAPS"]) == {"target": 40, "stoploss": -60}, f"GTT {P.gtt_for(P.STRATS['GAPS'])}")
    D = lambda d, h, m: datetime(2026, 10, d, h, m)

    class Kite:
        def instruments(self, ex):
            return [dict(segment="NFO-OPT", instrument_type=ty, name=n, strike=st, expiry=datetime(2026, 10, 27).date(), tradingsymbol=f"{n}26OCT{st}{ty}", lot_size=100)
                    for n in ("AAA", "BBB", "CCC", "DDD") for st in (45, 90, 95, 100, 105) for ty in ("CE", "PE")]

    class Rest:
        def __init__(self, nifty=TODAY, old=None): self.created, self.deleted, self.nifty = [], [], nifty; self.old = old or []
        def call(self, method, path, tries=1, **kw):
            if path == "/user/margins/equity": return True, {"net": 1000000.0, "available": {"live_balance": 1000000.0}}
            if path == "/alerts" and method == "GET": return True, [dict(uuid="oL", name="SHP|GAPL|OLD|10.00", status="enabled"), dict(uuid="oS", name="SHP|GAPS|OLD|10.00", status="enabled"), dict(uuid="swg", name="SWG|L|X|1", status="enabled")]
            if path == "/alerts" and method == "POST": self.created.append(kw["data"]); return True, {"uuid": "n"}
            if path == "/alerts" and method == "DELETE": self.deleted += [u for k, u in kw["params"] if k == "uuid"]; return True, {}
            if path == "/quote":
                out = {}
                for _, sym in kw["params"]:
                    out[sym] = {"last_trade_time": f"{self.nifty} 09:29:00"} if sym == "NSE:NIFTY 50" else dict(LIQUID_OPT)
                return True, out
            return False, "x"

    class FakeAng:
        calls = 0
        def day_quotes(self, tokens):
            FakeAng.calls += 1
            q = {"1": dict(ltp=100.5, low=99.0, high=104.0), "2": dict(ltp=103.0, low=100.0, high=104.0), "3": dict(ltp=96.0, low=95.5, high=99.0), "4": dict(ltp=97.0, low=96.0, high=99.0)}
            return {t: q[t] for t in tokens}

    ALL = [dict(stock="DDD", direction="LONG", ref=98.0, line=97.0, strategy="GAPL", last="x"),
           dict(stock="AAA", direction="SHORT", ref=98.0, line=97.0, strategy="GAPS", last="x"),      # untouched (low 99 > 98)
           dict(stock="BBB", direction="SHORT", ref=101.0, line=99.0, strategy="GAPS", last="x"),     # touched (low 100 <= 101) -> dropped at 09:30
           dict(stock="CCC", direction="SHORT", ref=95.0, line=94.0, strategy="GAPS", last="x")]      # untouched

    def run(now, dry="0", nifty=TODAY, price=20.0, active=None, fn="main", d=None, merge=False, setups=None):
        rest = Rest(nifty); d = d or tempfile.mkdtemp()
        P.now_ist, P.time.sleep, P.sleep_until = (lambda: now), (lambda s: None), (lambda h: None)
        P.PLAN_DIR, P.DRY_RUN, P.PLAN_MERGE = d, dry == "1", merge
        P.ROLL_DAYS, P.MARGIN_CHECK = 0, False
        P.login = lambda: (Kite(), "k", "t"); P.K = lambda k, t: rest; P.Angel = lambda: FakeAng()
        P.angel_master = lambda: ({"AAA": "1", "BBB": "2", "CCC": "3", "DDD": "4"}, {})
        P.ACTIVE = active or ["GAPL", "GAPS"]
        P.find_setups = lambda ang, eq: [x for x in (setups or ALL) if x["strategy"] in P.ACTIVE]
        P.option_prices = lambda ang, opt, picks: {i["tradingsymbol"]: price for sd, i in picks}
        try: getattr(P, fn)()
        except SystemExit as e: need(e.code in (0, None), f"plan exit {e.code}")
        return rest, d
    prm = lambda c: _j.loads(c["basket"])["items"][0]
    # ---- GAPL before the open: alert at once, no untouched check, CALL of the strike nearest the level
    FakeAng.calls = 0
    rest, d = run(D(19, 8, 40), active=["GAPL"])
    need([c["name"] for c in rest.created] == ["SHP|GAPL|DDD|98.00"], f"GAPL alert: {[c['name'] for c in rest.created]}")
    need(prm(rest.created[0])["tradingsymbol"] == "DDD26OCT100CE" and FakeAng.calls == 0, f"OTM CALL (first strike at or above 98): {prm(rest.created[0])['tradingsymbol']}, untouched-calls {FakeAng.calls}")
    need(rest.deleted == ["oL"], f"only the OLD GAPL alert is replaced (GAPS and SWG stay): {rest.deleted}")
    pa = prm(rest.created[0])["params"]; need(pa["quantity"] == 3500 and pa["gtt"] == {"target": 40, "stoploss": -60} and pa["tags"] == ["SHPGAPL"] and pa["transaction_type"] == "BUY", f"order params {pa}")
    need(_j.loads(rest.created[0]["basket"]).get("name") == "SHP|GAPL|DDD|98.00", "alert name in the basket")
    pl = _j.load(open(f"{d}/plan.json")); need(set(pl["strategies"]) == {"GAPL"} and pl["strategies"]["GAPL"]["max_open"] == 4 and len(pl["items"]) == 1, "plan.json GAPL")
    # ---- GAPS at 09:30: untouched check, PUT, merged into the same plan.json
    rest, d = run(D(19, 9, 30), active=["GAPS"], d=d, merge=True)
    need(sorted(c["name"] for c in rest.created) == ["SHP|GAPS|AAA|98.00", "SHP|GAPS|CCC|95.00"], f"GAPS alerts (BBB touched since the open is dropped): {[c['name'] for c in rest.created]}")
    need(sorted(prm(c)["tradingsymbol"] for c in rest.created) == ["AAA26OCT95PE", "CCC26OCT90PE"], f"OTM PUT (first strike strictly below the level): {[prm(c)['tradingsymbol'] for c in rest.created]}")
    need(rest.deleted == ["oS"], f"only the old GAPS alert replaced: {rest.deleted}")
    pl = _j.load(open(f"{d}/plan.json")); need(set(pl["strategies"]) == {"GAPL", "GAPS"} and sorted(i["strategy"] for i in pl["items"]) == ["GAPL", "GAPS", "GAPS"], f"merged plan: {[(i['strategy'], i['stock']) for i in pl['items']]}")
    need(all(i["qty"] == 3500 for i in pl["items"]), "7 % sizing")
    # ---- premium floor: Rs 5 and below never traded
    for px_, n_ in ((0.95, 0), (1.0, 0), (1.05, 2), (4.0, 2)):
        rest, d2 = run(D(19, 9, 30), active=["GAPS"], price=px_); need(len(rest.created) == n_, f"premium Rs {px_}: expected {n_} alerts, got {len(rest.created)}")
    need(P.LOW_MIN_PRICE == 1, f"floor {P.LOW_MIN_PRICE}")
    rest, d2 = run(D(19, 9, 30), dry="1")
    need(not rest.created and not rest.deleted and not os.path.exists(f"{d2}/plan.json"), "dry run must change nothing")
    rest, d2 = run(D(19, 9, 30), nifty="2026-10-16")
    need(not rest.created and _j.load(open(f"{d2}/plan.json"))["items"] == [], "holiday: empty plan, no alerts")
    # ---- evening preview (Mon 18:00 -> Tue; Fri 18:00 -> Mon), nothing created
    rest, d2 = run(D(19, 18, 0), fn="preview")
    pv = _j.load(open(f"{d2}/preview_next_2026-10-20.json"))
    need(not rest.created and not rest.deleted and not os.path.exists(f"{d2}/plan.json"), "preview must not create / delete / write plan.json")
    need(sorted((x["strategy"], x["stock"], x["tradable"]) for x in pv["items"]) == [("GAPL", "DDD", True), ("GAPS", "AAA", True), ("GAPS", "BBB", True), ("GAPS", "CCC", True)], f"preview items {[(x['strategy'], x['stock'], x['tradable']) for x in pv['items']]}")
    rest, d2 = run(datetime(2026, 10, 23, 18, 0, tzinfo=P.now_ist().tzinfo), fn="preview"); need(os.path.exists(f"{d2}/preview_next_2026-10-26.json"), "Friday evening -> Monday")
    rest, d2 = run(D(19, 18, 0), fn="preview", price=0.9)
    need(all(not x["tradable"] and "limit" in x["note"] for x in _j.load(open(f"{d2}/preview_next_2026-10-20.json"))["items"]), "options at or below Rs 1 not tradable in the preview")
    for now in (D(19, 8, 40), D(19, 9, 30), D(19, 13, 59)): need(str(P.plan_day(now)) == TODAY, f"plan_day({now})")
    print("PASS plan: GAPL alerts at once (OTM CALL, no untouched check) | GAPS at 09:30 only untouched setups (OTM PUT) | each step replaces only ITS strategy's alerts, SWG kept | merged plan.json | 7 % sizing, "
          "SL 60 / TP 40 GTT | Rs 1 premium floor | dry run | holiday | evening preview Fri -> Mon")


# ---------------------------------------------------------------------------------------------------------------- 3. LONG: cap 4, entry end, hand-over, 15:30
def check_long():
    LOGS.clear(); clock, fake = setup(job_end=dtime(9, 40))
    pl = plan(("AAA", "GAPL", 5000, 50), ("BBB", "GAPL", 5000, 50), ("CCC", "GAPL", 5000, 50), ("DDD", "GAPL", 5000, 50), ("EEE", "GAPL", 5000, 50), ("FFF", "GAPL", 5000, 50), ("GGG", "GAPL", 5000, 50))
    for s in "AAA BBB CCC DDD EEE FFF GGG".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
    arm_all(fake, pl); fake.add_alert("SWG|L|XYZ|10.00"); fake.add_alert("SHP|GAPS|ZZZ|9.00"); snap = {}
    def free():                                                       # AAA hit its target through the GTT
        fake.add_order("AAA", "SELL", "COMPLETE", "LIMIT", 70, 100, avg=70.0, tag=""); [g.__setitem__("status", "triggered") for g in fake.gtts if g["condition"]["tradingsymbol"] == "AAA"]
    for hh, mm, ss, fn in ((9, 16, 0, lambda: buy(fake, "AAA", "SHPGAPL")), (9, 16, 5, lambda: buy(fake, "BBB", "SHPGAPL")), (9, 16, 10, lambda: buy(fake, "CCC", "SHPGAPL")),
                           (9, 16, 15, lambda: buy(fake, "DDD", "SHPGAPL")), (9, 16, 30, lambda: snap.__setitem__("full", fake.alert_names())), (9, 18, 0, lambda: buy(fake, "EEE", "SHPGAPL")),
                           (9, 20, 0, free), (9, 22, 0, lambda: snap.__setitem__("freed", fake.alert_names())), (9, 30, 5, lambda: buy(fake, "GGG", "SHPGAPL"))):
        fake.at(hh, mm, ss, fn)
    ok = M.Manager(fake, pl).run(); need(ok, "morning job not ok")
    need(not [n for n in snap["full"] if n.startswith("SHP|GAPL|")], f"4 LONG trades open: GAPL alerts must be deleted: {snap['full']}")
    need("SWG|L|XYZ|10.00" in snap["full"], "SWG alert touched")
    need("EEE" in sold(fake) and at(9, 18) <= sold(fake)["EEE"][0] <= at(9, 18, 4) and "SHPCAP" in tags(fake, "EEE") and fake.qty("EEE") == 0, f"5th fill (EEE) must be sold at once as SHPCAP: {sold(fake).get('EEE')} {tags(fake, 'EEE')}")
    need("SHP|GAPL|FFF|100.00" in snap["freed"] and "SHP|GAPL|EEE|100.00" not in snap["freed"] and "SHP|GAPL|AAA|100.00" not in snap["freed"], f"slot free -> only the untraded FFF/GGG re-armed: {snap['freed']}")
    need("GGG" in sold(fake) and at(9, 30, 5) <= sold(fake)["GGG"][0] <= at(9, 30, 9) and "SHPLATE" in tags(fake, "GGG"), f"a BUY filled after the entry end 09:30:00 must be sold at once (SHPLATE): {sold(fake).get('GGG')}")
    need(not [n for n in fake.alert_names() if n.startswith("SHP|GAPL|")], f"GAPL alerts left after 09:30: {fake.alert_names()}")
    need(all(fake.qty(s) == 100 for s in ("BBB", "CCC", "DDD")) and fake.qty("AAA") == 0, "BBB / CCC / DDD stay open at the hand-over")
    need(any("hand-over" in l for l in LOGS), "no hand-over line")
    clock.t = at(15, 25); M.JOB_END = M.FINAL_END                      # the afternoon job: a NEW manager
    ok2 = M.Manager(fake, pl).run(); s30 = sold(fake)
    need(ok2 and all(at(15, 30) <= s30[s][0] <= at(15, 30, 2) for s in ("BBB", "CCC", "DDD")), f"afternoon job: square off at 15:30:00: {[(s, s30[s][0]) for s in ('BBB', 'CCC', 'DDD')]}")
    need(all(fake.qty(s) == 0 for s in "AAA BBB CCC DDD EEE FFF GGG".split()) and not any(g["status"] == "active" for g in fake.gtts), "something left open after 15:30")
    need("SWG|L|XYZ|10.00" in fake.alert_names(), "SWG alert touched")
    print("PASS LONG: cap 4 (5th fill sold SHPCAP, alerts deleted while full, re-armed when a slot freed) | BUY after 09:30:00 sold SHPLATE | hand-over | the afternoon job squares off at 15:30:00 clean | SWG untouched")


# ---------------------------------------------------------------------------------------------------------------- 4. SHORT: cap 3 and the dynamic 4th
def check_short():
    for scenario, bids, want4 in (("2 of 3 in profit / close -> 4th allowed", {"SS1": 50.0, "SS2": 49.0, "SS3": 40.0}, True), ("only 1 of 3 -> 4th refused", {"SS1": 50.0, "SS2": 40.0, "SS3": 40.0}, False)):
        LOGS.clear(); clock, fake = setup(start=(9, 29), job_end=dtime(9, 50))
        pl = plan(*[(f"SS{i}", "GAPS", 5000, 50) for i in range(1, 6)])
        for s in "SS1 SS2 SS3 SS4 SS5".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
        arm_all(fake, pl); snap = {}
        def fills(s): return lambda: buy(fake, s, "SHPGAPS")
        def price(): [fake.bid.__setitem__(k, v) for k, v in bids.items()]
        for hh, mm, ss, fn in ((9, 31, 0, fills("SS1")), (9, 32, 0, fills("SS2")), (9, 33, 0, fills("SS3")), (9, 33, 30, price), (9, 33, 50, lambda: snap.__setitem__("three", fake.alert_names())),
                               (9, 35, 0, fills("SS4")), (9, 36, 0, lambda: snap.__setitem__("after4", fake.alert_names())), (9, 37, 0, fills("SS5"))):
            fake.at(hh, mm, ss, fn)
        M.Manager(fake, pl).run()
        if want4:
            need("SHP|GAPS|SS4|100.00" in snap["three"] or "SHP|GAPS|SS5|100.00" in snap["three"], f"{scenario}: with 3 open and 2 in profit the alerts must stay: {snap['three']}")
            need(fake.qty("SS4") == 100 and "SS4" not in sold(fake), f"{scenario}: the 4th short must be kept")
            need(not [n for n in snap["after4"] if n.startswith("SHP|GAPS|")], f"{scenario}: 4 open -> alerts deleted: {snap['after4']}")
        else:
            need(not [n for n in snap["three"] if n.startswith("SHP|GAPS|")], f"{scenario}: 3 open and only 1 good -> alerts deleted: {snap['three']}")
            need("SS4" in sold(fake) and "SHPCAP" in tags(fake, "SS4") and fake.qty("SS4") == 0, f"{scenario}: the 4th fill must be sold SHPCAP: {tags(fake, 'SS4')}")
        need("SS5" in sold(fake) and "SHPCAP" in tags(fake, "SS5") and fake.qty("SS5") == 0, f"{scenario}: a 5th fill is always sold SHPCAP")
        need(all(fake.qty(s) == 100 for s in ("SS1", "SS2", "SS3")), f"{scenario}: the first three stay open")
    print("PASS SHORT: cap 3 | 4th trade allowed only while 2 of the 3 open shorts are in profit or within -5 % | never a 5th | alerts deleted when full")


# ---------------------------------------------------------------------------------------------------------------- 5. fund limit 30 % and loss stop
def check_fund_loss():
    LOGS.clear(); clock, fake = setup(start=(9, 29), job_end=dtime(9, 45), fund=30.0)
    pl = plan(("FF1", "GAPS", 40000, 50), ("FF2", "GAPS", 40000, 50), ("FF3", "GAPS", 40000, 50))
    for s in "FF1 FF2 FF3".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
    arm_all(fake, pl); snap = {}
    def used(x): fake.debits, fake.net = x, 700000.0 - x                # total always 7,00,000 -> limit 2,10,000
    for hh, mm, ss, fn in ((9, 30, 0, lambda: used(150000.0)), (9, 31, 0, lambda: buy(fake, "FF1", "SHPGAPS")), (9, 32, 0, lambda: used(250000.0)), (9, 32, 1, lambda: buy(fake, "FF2", "SHPGAPS")),
                           (9, 33, 0, lambda: snap.__setitem__("over", fake.alert_names())), (9, 34, 0, lambda: used(100000.0)), (9, 36, 0, lambda: snap.__setitem__("free", fake.alert_names()))):
        fake.at(hh, mm, ss, fn)
    M.Manager(fake, pl).run()
    need(fake.qty("FF1") == 100, "FF1 (used 21 %) must stay")
    need("FF2" in sold(fake) and "SHPFUND" in tags(fake, "FF2") and fake.qty("FF2") == 0, f"FF2: the fill pushed the account above 30 % -> sold SHPFUND: {tags(fake, 'FF2')}")
    need(not [n for n in snap["over"] if n.startswith("SHP|")], f"above 30 % all SHP alerts must be deleted: {snap['over']}")
    need("SHP|GAPS|FF3|100.00" in snap["free"] and "SHP|GAPS|FF2|100.00" not in snap["free"], f"funds free again -> FF3 re-armed, FF2 (already traded) not: {snap['free']}")
    LOGS.clear(); clock, fake = setup(start=(9, 29), job_end=dtime(9, 40), fund=30.0)
    pl = plan(("H1", "GAPS", 40000, 50), ("H2", "GAPS", 20000, 50)); arm_all(fake, pl); fake.debits, fake.net = 180000.0, 520000.0; snap = {}
    fake.at(9, 31, 0, lambda: snap.__setitem__("n", fake.alert_names())); M.Manager(fake, pl).run()
    need(snap["n"] == ["SHP|GAPS|H2|100.00"], f"headroom: used 1.8 lakh + 40,000 would pass 2.1 lakh -> H1 alert deleted, H2 (20,000) stays: {snap['n']}")
    print("PASS fund limit 30 %: a fill that crosses it is sold SHPFUND | alerts deleted while above | re-armed when free (traded setup stays off)")
    # ---- loss stop
    LOGS.clear(); clock, fake = setup(start=(9, 29), job_end=dtime(9, 45), loss=30.0)
    pl = plan(("LL1", "GAPS", 5000, 50), ("LL2", "GAPS", 5000, 50), ("LL3", "GAPS", 5000, 50), ("LL4", "GAPS", 5000, 50))
    for s in "LL1 LL2 LL3 LL4".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
    arm_all(fake, pl); snap = {}
    def pnl(a, b): fake.pnl["LL1"], fake.pnl["LL2"] = a, b
    for hh, mm, ss, fn in ((9, 31, 0, lambda: buy(fake, "LL1", "SHPGAPS")), (9, 31, 5, lambda: buy(fake, "LL2", "SHPGAPS")), (9, 31, 30, lambda: pnl(-1000.0, -500.0)),
                           (9, 31, 50, lambda: snap.__setitem__("small", fake.alert_names())), (9, 32, 10, lambda: pnl(-2000.0, -1500.0)), (9, 32, 30, lambda: snap.__setitem__("stopped", fake.alert_names())),
                           (9, 33, 0, lambda: buy(fake, "LL3", "SHPGAPS")), (9, 34, 0, lambda: fake.pos.__setitem__(("LL1", "NRML"), 0)), (9, 35, 0, lambda: snap.__setitem__("later", fake.alert_names()))):
        fake.at(hh, mm, ss, fn)
    M.Manager(fake, pl).run()
    need([n for n in snap["small"] if n.startswith("SHP|GAPS|LL")], f"loss Rs 1,500 of Rs 10,000 invested = 15 %: trading goes on: {snap['small']}")
    need(not [n for n in snap["stopped"] if n.startswith("SHP|")], f"loss Rs 3,500 > 30 % of Rs 10,000: alerts must be deleted: {snap['stopped']}")
    need("LL3" in sold(fake) and "SHPLOSS" in tags(fake, "LL3") and fake.qty("LL3") == 0, f"a fill after the loss stop is sold SHPLOSS: {tags(fake, 'LL3')}")
    need(fake.qty("LL2") == 100 and not [n for n in snap["later"] if n.startswith("SHP|")], f"open trades stay, nothing re-armed after the stop: {snap['later']}")
    need(any("LOSS STOP" in l for l in LOGS), "no LOSS STOP log")
    print("PASS loss stop: 15 % loss keeps trading | loss above 30 % of the money invested deletes the alerts, sells later fills (SHPLOSS), keeps open trades and never re-arms")


# ---------------------------------------------------------------------------------------------------------------- 6. plan reload, recovery, stale, holiday, stuck market order, dry run
def check_reload():
    import tempfile, json as _j
    LOGS.clear(); clock, fake = setup(job_end=dtime(9, 45))
    pf = os.path.join(tempfile.mkdtemp(), "plan.json"); empty = dict(plan(), trade_day=TODAY, strategies={"GAPL": dict(RULES["GAPL"])}); _j.dump(empty, open(pf, "w")); os.utime(pf, (1_900_000_000, 1_900_000_000))
    M.PLAN = pf; fake.bid["TT1"], fake.ask["TT1"] = 50.0, 50.5; fake.bid["TTT"], fake.ask["TTT"] = 50.0, 50.5
    def newplan():
        pl2 = plan(("TT1", "GAPS", 5000, 50)); pl2["strategies"] = {"GAPS": dict(RULES["GAPS"])}; _j.dump(pl2, open(pf, "w")); os.utime(pf, (1_900_000_100, 1_900_000_100)); arm_all(fake, pl2)
    fake.at(9, 30, 20, newplan); fake.at(9, 35, 0, lambda: buy(fake, "TT1", "SHPGAPS"))
    m = M.Manager(fake, empty); m.run()
    need("TT1" in m.items and m.trades.get("TT1", {}).get("strat") == "GAPS" and fake.qty("TT1") == 100, f"the GAPS item added at 09:30 must be managed: {list(m.items)} {m.trades.keys()}")
    need(any("plan reloaded" in l for l in LOGS) and any("ENTRY 100 @ 50.0" in l and "GAPS" in l for l in LOGS), "reload / entry log")
    clock.t = at(15, 25); M.JOB_END = M.FINAL_END; ok = M.Manager(fake, dict(empty, items=[dict(plan(("TT1", "GAPS", 5000, 50))["items"][0])], strategies={"GAPS": dict(RULES["GAPS"])})).run()
    need(ok and fake.qty("TT1") == 0 and at(15, 30) <= sold(fake)["TT1"][0] <= at(15, 30, 2), "TT1 squared off 15:30:00")
    print("PASS plan reload: a GAPS plan written at 09:30 while the manager runs is picked up, its fill managed and squared off at 15:30:00")


def check_main_paths():
    import tempfile, json as _j
    def prep(start, kind):
        LOGS.clear(); clock, fake = setup(start=start)
        pf = os.path.join(tempfile.mkdtemp(), "plan.json")
        if kind == "old": _j.dump({"trade_day": "2026-10-16", "items": []}, open(pf, "w"))
        if kind == "empty": _j.dump({"trade_day": TODAY, "strategies": {}, "items": []}, open(pf, "w"))
        M.PLAN, M.login, M.K = pf, (lambda: (None, "k", "t")), (lambda k, t: fake)
        M.sys.argv = ["gap_live_manager.py"]; os.environ.pop("JOB_END", None)
        return clock, fake, pf
    for kind in ("old", "missing"):                                                  # stale / missing plan: unmanaged SHP| alerts deleted, job fails
        clock, fake, pf = prep((11, 50), kind); fake.add_alert("SHP|GAPS|AAA|100.00"); fake.add_alert("SWG|L|XYZ|10.00")
        try: M.main(); need(False, "a stale / missing plan must exit non-zero")
        except SystemExit as e: need(e.code not in (0, None), f"{kind}: exit {e.code}")
        need(fake.alert_names() == ["SWG|L|XYZ|10.00"], f"{kind}: {fake.alert_names()}")
    clock, fake, pf = prep((9, 45), "empty"); M.main(); need(fake.mutations == 0 and any("nothing to manage" in l for l in LOGS), "no items after 09:40: nothing to do")
    for kind in ("no plan", "plan without GAPS"):                                      # recovery from the order tags
        clock, fake, pf = prep((15, 25), "missing" if kind == "no plan" else "empty")
        if kind == "plan without GAPS": _j.dump(plan(("CCC", "GAPL", 4000, 40)), open(pf, "w")); buy(fake, "CCC", "SHPGAPL", 40.0, ts="2026-10-19 09:16:00")
        for s in ("AAA", "CCC"): fake.bid[s], fake.ask[s] = 40.0, 40.5
        fake.add_alert("SHP|GAPS|ZZZ|9.00"); fake.add_alert("SWG|L|XYZ|10.00"); buy(fake, "AAA", "SHPGAPS", 40.0, ts="2026-10-19 10:30:00")
        M.main(); s_ = sold(fake)
        need(any("RECOVERY: AAA [GAPS]" in l for l in LOGS), f"{kind}: not recovered"); need(at(15, 30) <= s_["AAA"][0] <= at(15, 30, 2) and fake.qty("AAA") == 0, f"{kind}: AAA sold {s_.get('AAA')}")
        if kind == "plan without GAPS": need(fake.qty("CCC") == 0 and at(15, 30) <= s_["CCC"][0] <= at(15, 30, 2), "the planned trade is still managed")
        need(fake.alert_names() == ["SWG|L|XYZ|10.00"] and not any(g["status"] == "active" for g in fake.gtts), f"{kind}: {fake.alert_names()}")
    print("PASS stale / missing plan deletes SHP| alerts and fails | empty plan after 09:40 does nothing | open trades without a plan are recovered from SHPGAPL / SHPGAPS tags and squared off at 15:30:00")


def check_misc():
    LOGS.clear(); clock, fake = setup(start=(15, 10))
    pl = plan(("CCC", "GAPS", 4000, 40), ("DDD", "GAPS", 4000, 40), ("EEE", "GAPL", 4000, 40))
    for s in "CCC DDD EEE".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    arm_all(fake, pl); fake.market_stuck = {"DDD"}; fake.bid["DDD"], fake.ask["DDD"] = 35.0, 35.5
    buy(fake, "CCC", "SHPGAPS", 40.0, ts="2026-10-19 11:00:00"); buy(fake, "DDD", "SHPGAPS", 40.0, ts="2026-10-19 11:05:00"); fake.move("EEE", "NRML", 100)
    fake.at(15, 12, 0, lambda: fake.add_order("EEE", "SELL", "OPEN", "LIMIT", 99, 100, tag=""))
    ok = M.Manager(fake, pl).run(); s_ = sold(fake)
    need(ok and at(15, 30) <= s_["CCC"][0] <= at(15, 30, 1), "CCC 15:30:00"); need(s_["DDD"][1]["order_type"] == "LIMIT" and at(15, 30) <= s_["DDD"][0] <= at(15, 30, 12), "stuck market order closed by the aggressive limit")
    need(all(fake.qty(s) == 0 and not fake.open_orders(s) for s in ("CCC", "DDD", "EEE")) and not fake.alert_names() and not any(g["status"] == "active" for g in fake.gtts), "something left")
    # holiday
    LOGS.clear(); clock, fake = setup(start=(9, 5), job_end=dtime(9, 40)); fake.nifty_day = "2026-10-16"
    pl = plan(("AAA", "GAPL", 4000, 40)); arm_all(fake, pl); fake.add_alert("SWG|L|XYZ|10.00"); M.Manager(fake, pl).run()
    need(fake.alert_names() == ["SWG|L|XYZ|10.00"] and any("No trading today" in l for l in LOGS), f"holiday: {fake.alert_names()}")
    # dry run
    LOGS.clear(); clock, fake = setup(dry=True, start=(15, 25)); pl = plan(("AAA", "GAPL", 4000, 40)); arm_all(fake, pl); fake.bid["AAA"], fake.ask["AAA"] = 40.0, 40.5; buy(fake, "AAA", "SHPGAPL", 40.0, ts="2026-10-19 09:16:00")
    n0 = fake.mutations; M.Manager(fake, pl).run(); need(fake.mutations == n0 and fake.qty("AAA") == 100, "DRY_RUN must send nothing")
    # job 2 starting after the GAPS entry end (14:00): its waiting alerts and BUYs must be removed at once
    LOGS.clear(); clock, fake = setup(start=(14, 0, 20)); pl = plan(("AAA", "GAPS", 4000, 40), ("BBB", "GAPS", 4000, 40)); arm_all(fake, pl); fake.bid["AAA"], fake.ask["AAA"] = 40.0, 40.5
    fake.at(14, 0, 25, lambda: fake.add_order("BBB", "BUY", "OPEN", "LIMIT", 30, 100, tag="SHPGAPS")); buy(fake, "AAA", "SHPGAPS", 40.0, ts="2026-10-19 10:00:00")
    M.JOB_END = dtime(14, 5); M.Manager(fake, pl).run()
    need(not fake.alert_names() and not fake.open_orders("BBB") and fake.qty("AAA") == 100, f"late-started job must clear GAPS alerts / waiting BUYs, keep the position: {fake.alert_names()}")
    print("PASS misc: stuck market order closed by the aggressive limit at 15:30 | leftover orders cleared | holiday deletes alerts | DRY_RUN sends nothing")


if __name__ == "__main__":
    check_setups(); check_plan(); check_long(); check_short(); check_fund_loss(); check_reload(); check_main_paths(); check_misc(); print("ALL OFFLINE TESTS PASSED")
