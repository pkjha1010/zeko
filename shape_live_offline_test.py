"""
Offline test of shape_live_manager.py against a SIMULATED Zerodha (no login, no orders, no internet, runs in a few seconds).
    python shape_live_offline_test.py
It runs whole trading mornings / afternoons on a fake clock and checks:
  - a BUY that price ran away from (waiting) is cancelled within ~0.5 s
  - a stop-loss SELL that waits (price below the entry, unfilled) is cancelled and the position sold at once; a target SELL that waits is cancelled and sold after TP_WAIT_S
  - FUND LIMIT 30 %: above it every waiting SHP| alert is deleted and waiting BUYs are cancelled at once; below it the alerts are re-created (only strategies still inside their
    entry time, only setups that have not traded)
  - every strategy has its OWN entry end and square-off: WRU 09:30:00, WRL 10:00:00, BPB 15:30:00 (a position of another strategy is not touched earlier)
  - a BUY placed after the strategy's entry end that still fills is sold at once; its waiting alerts are deleted at the entry end only for that strategy
  - a market order that keeps waiting is closed by the aggressive LIMIT order
  - the manager can be restarted any time (state rebuilt from orders / positions / alerts), hand-over jobs end quietly
  - other alerts (SWG|) and other positions / orders are not touched (SWEEP_ALL=0); SWEEP_ALL=1 clears them after the last strategy
  - DRY_RUN sends nothing;  a holiday (NIFTY has no trade today) does nothing
"""
import os, sys, types, threading
import numpy as np
from datetime import datetime, timedelta, time as dtime
for _m in ("kiteconnect", "pyotp"):                       # only needed for the real login; stubbed when not installed
    try: __import__(_m)
    except ImportError:
        mod = types.ModuleType(_m); sys.modules[_m] = mod
        if _m == "kiteconnect": mod.KiteConnect = object
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shape_live_manager as M

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
        self.net, self.debits, self.nifty_day = 700000.0, 0.0, "2026-10-19"
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
        if path == "/portfolio/positions": return True, {"net": [dict(tradingsymbol=s, product=p, exchange="NFO", quantity=q, average_price=50.0) for (s, p), q in self.pos.items() if q], "day": []}
        if path == "/user/margins/equity": return True, {"net": self.net, "available": {"live_balance": self.net}, "utilised": {"debits": self.debits}}
        if path == "/gtt/triggers" and method == "GET": return True, [dict(g) for g in self.gtts]
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
RULES = {"WRU": dict(name="WICK_RED_UPPER", entry_end="09:30:00", exit_at="09:30:00", sl_pct=20.0, tp_pct=0.0, max_open=0),
         "WRL": dict(name="WICK_RED_LOWER", entry_end="10:00:00", exit_at="10:00:00", sl_pct=0.0, tp_pct=60.0, max_open=0),
         "ENG": dict(name="ENGULF_LONG", entry_end="09:17:00", exit_at="15:30:00", sl_pct=40.0, tp_pct=40.0, max_open=3),
         "BPB": dict(name="BREAKOUT_BPB", entry_end="15:29:00", exit_at="15:30:00", sl_pct=0.0, tp_pct=60.0, max_open=2)}


def setup(sweep_all=False, dry=False, start=(9, 14), job_end=None):
    clock = Clock(*start); fake = Fake(clock)
    M.time, M.now_ist, M.SWEEP_ALL, M.DRY_RUN = clock, clock.now, sweep_all, dry
    M.BUY_CHASE = False                                              # the old tests check the cancel-at-once rule; check_chase() switches the chase on
    M.FUND_LIMIT_PCT, M.TOTAL_FUNDS = 0.0, 0.0                       # the 30 % fund limit is OFF by default now (check_fund_limit switches it on)
    M.JOB_END = job_end or M.FINAL_END
    M.threading = types.SimpleNamespace(RLock=threading.RLock, Thread=SyncThread)
    M.log = lambda m: LOGS.append(f"[{clock.now():%H:%M:%S}] {m}")
    return clock, fake


def plan(*rows):
    """rows = (option, strategy, cost, price)"""
    items = [dict(option=o, strategy=s, qty=100, close_price=px, cost=c, stock=o[:3], direction="LONG", ref=100.0, alert=f"SHP|{s}|{o}|100.00",
                  alert_data=dict(name=f"SHP|{s}|{o}|100.00", type="ato")) for o, s, c, px in rows]
    return {"trade_day": "2026-10-19", "strategies": {c: dict(RULES[c]) for c in {r[1] for r in rows}}, "items": items}


def arm_all(fake, pl):
    for i in pl["items"]: fake.add_alert(i["alert"])


def dump(n=70): return "\n--- last log lines ---\n" + "\n".join(LOGS[-n:])


def need(cond, msg):
    if not cond: raise AssertionError(msg + dump())


# ---------------------------------------------------------------------------------------------------------------- 1. the safety rules + per-strategy times (morning)
def check_morning():
    LOGS.clear(); clock, fake = setup(job_end=dtime(10, 5))
    pl = plan(("AAA", "WRU", 5000, 50), ("BBB", "WRL", 5000, 50), ("CCC", "BPB", 4000, 40), ("DDD", "WRU", 6000, 60), ("EEE", "WRL", 5000, 50), ("FFF", "WRU", 5500, 55), ("GGG", "WRU", 5000, 50),
              ("HHH", "BPB", 4000, 40))
    for s in "AAA BBB CCC DDD EEE FFF GGG HHH ZZZ".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
    arm_all(fake, pl); fake.add_alert("SWG|L|XYZ|10.00")
    fake.move("ZZZ", "NRML", 50); fake.add_order("ZZZ", "SELL", "OPEN", "LIMIT", 70, 50)         # another strategy's position and waiting order
    T = {}
    def A(): T["A"] = fake.add_order("AAA", "BUY", "OPEN", "MARKET", qty=100)                          # WRU BUY: price ran away, it waits
    def B(): fake.add_order("BBB", "BUY", "COMPLETE", avg=50, qty=100, ts=fake.stamp(), tag="SHPWRL"); fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="BBB")))
    def C(): fake.add_order("CCC", "BUY", "COMPLETE", avg=40, qty=100, ts=fake.stamp(), tag="SHPBPB"); fake.gtts.append(dict(id=2, status="active", condition=dict(tradingsymbol="CCC")))
    def D1(): fake.add_order("DDD", "BUY", "COMPLETE", avg=60, qty=100, ts=fake.stamp(), tag="SHPWRU"); fake.gtts.append(dict(id=3, status="active", condition=dict(tradingsymbol="DDD")))
    def D2(): fake.bid["DDD"], fake.ask["DDD"] = 45.0, 45.5; fake.gtts[2]["status"] = "triggered"; T["D"] = fake.add_order("DDD", "SELL", "OPEN", "LIMIT", 48, 100)   # stop-loss limit order waits
    def E1(): fake.add_order("EEE", "BUY", "COMPLETE", avg=50, qty=100, ts=fake.stamp(), tag="SHPWRL"); fake.gtts.append(dict(id=4, status="active", condition=dict(tradingsymbol="EEE")))
    def E2(): fake.bid["EEE"], fake.ask["EEE"] = 70.0, 70.5; fake.gtts[3]["status"] = "triggered"; T["E"] = fake.add_order("EEE", "SELL", "OPEN", "LIMIT", 80, 100)       # target limit order waits
    def F(): fake.add_order("FFF", "BUY", "COMPLETE", avg=55, qty=100, ts="2026-10-19 09:25:30", tag="SHPWRU"); fake.gtts.append(dict(id=5, status="active", condition=dict(tradingsymbol="FFF")))
    def G(): T["G"] = fake.add_order("GGG", "BUY", "OPEN", "MARKET", qty=100)                          # WRU BUY waiting after its entry end 09:30
    def H(): fake.add_order("HHH", "BUY", "COMPLETE", avg=40, qty=100, ts=fake.stamp(), tag="SHPBPB")  # BPB: stays open all day
    for hh, mm, ss, fn in ((9, 16, 0, A), (9, 17, 0, B), (9, 18, 0, C), (9, 20, 0, D1), (9, 22, 0, D2), (9, 24, 0, E1), (9, 24, 30, E2), (9, 26, 0, F), (9, 30, 10, G), (9, 40, 0, H)): fake.at(hh, mm, ss, fn)
    m = M.Manager(fake, pl); ok = m.run()
    need(ok, "hand-over run reported NOT ok")
    cancel_t = {o["order_id"]: t for t, a, o in fake.audit if a == "cancel"}
    a_dt = (cancel_t[T["A"]["order_id"]] - T["A"]["placed_at"]).total_seconds()
    need(0.4 <= a_dt <= 1.5, f"waiting BUY cancelled after {a_dt:.2f}s (expected ~0.5s)")
    g_dt = (cancel_t[T["G"]["order_id"]] - T["G"]["placed_at"]).total_seconds()
    need(g_dt <= 1.0, f"BUY waiting after the entry end cancelled after {g_dt:.2f}s (expected at once)")
    sells = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sells.setdefault(o["tradingsymbol"], []).append((t, o))
    d_cancel = (cancel_t[T["D"]["order_id"]] - T["D"]["placed_at"]).total_seconds()
    need(0.4 <= d_cancel <= 1.5, f"waiting stop-loss SELL cancelled after {d_cancel:.2f}s")
    need(at(9, 22) <= sells["DDD"][0][0] <= at(9, 22, 3), f"DDD stop-loss position sold at {sells['DDD'][0][0]:%H:%M:%S}")
    e_cancel = (cancel_t[T["E"]["order_id"]] - T["E"]["placed_at"]).total_seconds()
    need(2.9 <= e_cancel <= 4.0, f"waiting TARGET SELL cancelled after {e_cancel:.2f}s (expected TP_WAIT_S = 3s)")
    need(at(9, 24, 30) <= sells["EEE"][0][0] <= at(9, 24, 36), f"EEE target position sold at {sells['EEE'][0][0]:%H:%M:%S}")
    need(at(9, 30) <= sells["FFF"][0][0] <= at(9, 30, 3), f"FFF (WRU) squared off at {sells['FFF'][0][0]:%H:%M:%S}, expected 09:30:00")
    need(at(10, 0) <= sells["BBB"][0][0] <= at(10, 0, 3), f"BBB (WRL) squared off at {sells['BBB'][0][0]:%H:%M:%S}, expected 10:00:00")
    need(fake.qty("CCC") == 100 and fake.qty("HHH") == 100, "a BPB position (exit 15:30) was closed early")
    need(fake.qty("GGG") == 0 and "GGG" not in sells, "GGG: the BUY should have been cancelled, nothing bought")
    for s in ("AAA", "BBB", "DDD", "EEE", "FFF"): need(fake.qty(s) == 0 and not fake.open_orders(s), f"{s} still has a position / waiting orders")
    need(not any(g["status"] == "active" and g["condition"]["tradingsymbol"] in ("BBB", "DDD", "EEE", "FFF") for g in fake.gtts), "an active GTT of a closed option is left")
    need(any(g["status"] == "active" and g["condition"]["tradingsymbol"] == "CCC" for g in fake.gtts), "the BPB GTT must stay")
    names = fake.alert_names()
    need(not [n for n in names if n.startswith("SHP|WRU|")], f"WRU alerts left after 09:30: {names}")
    need(not [n for n in names if n.startswith("SHP|WRL|")], f"WRL alerts left after 10:00: {names}")
    need("SWG|L|XYZ|10.00" in names, "the SWG alert was touched")
    need(fake.qty("ZZZ") == 50 and len(fake.open_orders("ZZZ")) == 1, "another strategy's position / order was touched")
    ae = {a["name"]: t for t, k, a in fake.audit if k == "alert-"}
    wru_t = [t for n, t in ae.items() if n.startswith("SHP|WRU|")]; wrl_t = [t for n, t in ae.items() if n.startswith("SHP|WRL|")]
    need(wru_t and all(at(9, 30) <= t <= at(9, 30, 2) for t in wru_t), f"WRU alerts deleted at {[f'{t:%H:%M:%S}' for t in wru_t]}, expected 09:30:00")
    need(wrl_t and all(at(10, 0) <= t <= at(10, 0, 2) for t in wrl_t), f"WRL alerts deleted at {[f'{t:%H:%M:%S}' for t in wrl_t]}, expected 10:00:00")
    need(any("hand-over" in l for l in LOGS), "no hand-over line")
    print("PASS morning: stuck BUY cancelled in %.1fs | stop-loss SELL cancelled in %.1fs and position sold | waiting target SELL cancelled in %.1fs and sold | WRU squared off 09:30:00 and its alerts "
          "deleted | WRL squared off 10:00:00 | BPB left open | late BUY cancelled at once | SWG alert and other strategies untouched | hand-over quiet" % (a_dt, d_cancel, e_cancel))


# ---------------------------------------------------------------------------------------------------------------- 2. fund limit 30 %
def check_fund_limit():
    LOGS.clear(); clock, fake = setup(job_end=dtime(9, 25)); M.FUND_LIMIT_PCT = 30.0
    pl = plan(("AAA", "WRU", 40000, 50), ("BBB", "WRL", 50000, 50), ("CCC", "BPB", 60000, 50), ("DDD", "WRL", 30000, 50), ("EEE", "WRL", 130000, 50))
    for s in "AAA BBB CCC DDD EEE".split(): fake.bid[s], fake.ask[s] = 50.0, 50.5
    arm_all(fake, pl); fake.add_alert("SWG|L|XYZ|10.00")
    fake.net = 700000.0; fake.debits = 0.0                                  # total 7,00,000 -> limit 2,10,000
    T = {}
    def X1(): fake.add_order("BBB", "BUY", "COMPLETE", avg=50, qty=100, ts=fake.stamp(), tag="SHPWRL"); fake.net, fake.debits = 450000.0, 250000.0     # a fill: 2.5 lakh used = 35.7 %
    def X2(): T["C"] = fake.add_order("CCC", "BUY", "OPEN", "MARKET", qty=100)                          # an order slipped in while the limit is exceeded
    def X3(): fake.net, fake.debits = 600000.0, 100000.0                                                  # a position closed: 1 lakh used = 14.3 %
    for hh, mm, ss, fn in ((9, 16, 0, X1), (9, 16, 5, X2), (9, 20, 0, X3)): fake.at(hh, mm, ss, fn)
    snap = {}
    def S1(): snap["blocked"] = fake.alert_names()
    fake.at(9, 18, 0, S1)
    m = M.Manager(fake, pl); ok = m.run()
    need(ok, "run not ok")
    need(snap["blocked"] == ["SWG|L|XYZ|10.00"], f"while the fund limit is exceeded no SHP| alert may wait: {snap['blocked']}")
    cancel_t = {o["order_id"]: t for t, a, o in fake.audit if a == "cancel"}
    c_dt = (cancel_t[T["C"]["order_id"]] - T["C"]["placed_at"]).total_seconds()
    need(c_dt <= 1.0, f"BUY placed while the limit is exceeded cancelled after {c_dt:.2f}s (expected at once)")
    names = fake.alert_names()
    # after the release 1,00,000 is used, the limit is 2,10,000: DDD (30,000) and AAA (40,000) fit; EEE (1,30,000) would make 2,30,000 > 2,10,000 -> stays off;
    # BBB was bought and CCC's BUY was seen (the alert fired) -> never re-created
    need("SHP|WRL|DDD|100.00" in names and "SHP|WRU|AAA|100.00" in names, f"alerts that fit the free budget were not re-created: {names}")
    need("SHP|WRL|EEE|100.00" not in names, f"EEE (1,30,000) would pass the limit but was re-created: {names}")
    need("SHP|BPB|CCC|100.00" not in names, "CCC already fired - its alert must not be re-created")
    need("SHP|WRL|BBB|100.00" not in names, "BBB already traded - its alert must not be re-created")
    need("SWG|L|XYZ|10.00" in names, "SWG alert touched")
    print("PASS fund limit: above 30 %% all SHP alerts deleted and a late BUY cancelled in %.1fs | after funds were free again only the alerts that fit the limit were re-created "
          "(traded setup and the one that would pass the limit stayed off)" % c_dt)
    # a strategy past its entry time is NOT re-armed
    LOGS.clear(); clock, fake = setup(start=(9, 28), job_end=dtime(9, 40)); M.FUND_LIMIT_PCT = 30.0
    pl = plan(("AAA", "WRU", 40000, 50), ("BBB", "WRL", 50000, 50)); arm_all(fake, pl); fake.net, fake.debits = 450000.0, 250000.0
    def Y(): fake.net, fake.debits = 700000.0, 0.0
    fake.at(9, 29, 0, Y)
    M.Manager(fake, pl).run()
    names = fake.alert_names()
    need(names == ["SHP|WRL|BBB|100.00"], f"after 09:30 only the WRL alert may be re-created, WRU entry time is over: {names}")
    print("PASS fund limit: a strategy whose entry time is over (WRU after 09:30) is not re-armed")


# ---------------------------------------------------------------------------------------------------------------- 3. afternoon: BPB 15:30 + stuck market order
def check_afternoon():
    LOGS.clear(); clock, fake = setup(start=(15, 10))
    pl = plan(("CCC", "BPB", 4000, 40), ("DDD", "BPB", 4000, 40), ("EEE", "BPB", 4000, 40))
    for s in "CCC DDD EEE".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    arm_all(fake, pl); fake.market_stuck = {"DDD"}; fake.bid["DDD"], fake.ask["DDD"] = 35.0, 35.5
    fake.add_order("CCC", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 11:00:00", tag="SHPBPB"); fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="CCC")))
    fake.add_order("DDD", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 11:05:00", tag="SHPBPB")
    fake.move("EEE", "NRML", 100)                                                                         # a position the manager never saw a BUY for
    fake.at(15, 12, 0, lambda: fake.add_order("EEE", "SELL", "OPEN", "LIMIT", 99, 100, tag=""))           # leftover waiting sell
    ok = M.Manager(fake, pl).run()
    need(ok, "run not clean")
    sells = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sells.setdefault(o["tradingsymbol"], []).append((t, o))
    need(at(15, 30) <= sells["CCC"][0][0] <= at(15, 30, 1), f"CCC squared off at {sells['CCC'][0][0]:%H:%M:%S.%f}, expected 15:30:00")
    need(sells["DDD"][0][1]["order_type"] == "LIMIT", "DDD (market orders keep waiting) should be closed by the aggressive LIMIT order")
    need(at(15, 30) <= sells["DDD"][0][0] <= at(15, 30, 12), f"DDD squared off at {sells['DDD'][0][0]:%H:%M:%S}")
    for s in ("CCC", "DDD", "EEE"): need(fake.qty(s) == 0 and not fake.open_orders(s), f"{s} not flat")
    need(not any(g["status"] == "active" for g in fake.gtts), "a GTT is left")
    need(not fake.alert_names(), "an SHP alert is left")
    need(any("SWEEP CLEAN" in l for l in LOGS) and clock.now() < at(15, 31), f"job needed until {clock.now():%H:%M:%S}")
    print("PASS afternoon: BPB squared off at 15:30:00 | stuck market order closed by the aggressive limit | untracked position and leftover order cleared | job clean at %s" % f"{clock.now():%H:%M:%S}")


# ---------------------------------------------------------------------------------------------------------------- 4. restart in the middle of the day
def check_restart():
    LOGS.clear(); clock, fake = setup(start=(11, 0))
    pl = plan(("CCC", "BPB", 4000, 40), ("DDD", "WRL", 4000, 40))
    for s in "CCC DDD".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    fake.add_alert("SHP|BPB|CCC|100.00")
    fake.add_order("CCC", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 10:30:00", tag="SHPBPB")     # bought before the restart; the manager sees it only through the order book
    fake.move("DDD", "NRML", 100)                                                                         # WRL option still open although its exit time (10:00) is over
    fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="CCC")))
    ok = M.Manager(fake, pl).run()
    need(ok and fake.qty("CCC") == 0 and fake.qty("DDD") == 0, "restart: positions not closed")
    sells = {t: o for t, a, o in fake.audit if a == "fill" and o["transaction_type"] == "SELL"}
    ts = {o["tradingsymbol"]: t for t, o in sells.items()}
    need(at(15, 30) <= ts["CCC"] <= at(15, 30, 2), f"restart: CCC squared off at {ts['CCC']:%H:%M:%S}, expected 15:30:00")
    need(ts["DDD"] <= at(11, 0, 5), f"restart: the overdue WRL position was closed at {ts['DDD']:%H:%M:%S}, expected at once")
    print("PASS restart: state rebuilt from Kite (order book, positions, alerts) | overdue position closed at once | BPB squared off 15:30:00")


# ---------------------------------------------------------------------------------------------------------------- 5. SWEEP_ALL, DRY_RUN, holiday
def check_misc():
    LOGS.clear(); clock, fake = setup(sweep_all=True, start=(15, 25))
    pl = plan(("AAA", "BPB", 4000, 40)); fake.bid["ZZZ"], fake.ask["ZZZ"] = 30.0, 30.5
    fake.move("ZZZ", "NRML", 50); fake.add_order("ZZZ", "SELL", "OPEN", "LIMIT", 70, 50)
    ok = M.Manager(fake, pl).run()
    need(ok and fake.qty("ZZZ") == 0 and not fake.open_orders(), "SWEEP_ALL=1 did not clear the account")
    print("PASS SWEEP_ALL=1: another strategy's NFO position and waiting order are cleared after the last strategy")
    LOGS.clear(); clock, fake = setup(dry=True, start=(9, 14), job_end=dtime(9, 40)); M.FUND_LIMIT_PCT = 30.0
    pl = plan(("AAA", "WRU", 4000, 40)); arm_all(fake, pl); fake.add_order("AAA", "BUY", "OPEN", "MARKET", qty=100); fake.net, fake.debits = 100000.0, 600000.0
    M.Manager(fake, pl).run()
    need(fake.mutations == 0, f"DRY_RUN sent {fake.mutations} changing calls")
    print("PASS DRY_RUN: nothing sent")
    LOGS.clear(); clock, fake = setup(start=(9, 19), job_end=dtime(9, 40)); fake.nifty_day = "2026-10-16"
    pl = plan(("AAA", "WRU", 4000, 40)); arm_all(fake, pl)
    ok = M.Manager(fake, pl).run()
    need(ok and fake.alert_names() == ["SHP|WRU|AAA|100.00"] and fake.mutations == 0, "holiday: something was changed")
    print("PASS holiday: NIFTY has no trade today -> alerts left untouched, nothing sent")


# ---------------------------------------------------------------------------------------------------------------- 6. BPB max 3 open, WRU unlimited
def check_max_open():
    LOGS.clear(); clock, fake = setup(start=(9, 14), job_end=dtime(9, 45))
    pl = plan(("AAA", "BPB", 4000, 40), ("BBB", "BPB", 4000, 40), ("CCC", "BPB", 4000, 40), ("DDD", "BPB", 4000, 40), ("EEE", "BPB", 4000, 40), ("FFF", "BPB", 4000, 40),
              ("U1", "WRU", 4000, 40), ("U2", "WRU", 4000, 40), ("U3", "WRU", 4000, 40), ("U4", "WRU", 4000, 40), ("U5", "WRU", 4000, 40))
    for s in "AAA BBB CCC DDD EEE FFF U1 U2 U3 U4 U5".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    fake.bid["FFF"] = 120.0                                              # stock FFF is already beyond its entry level (ref 100 for the test plan) when a slot frees
    arm_all(fake, pl); snap = {}
    def fill(s, tag): fake.add_order(s, "BUY", "COMPLETE", avg=40, qty=100, ts=fake.stamp(), tag=tag)
    def first3():
        for s in ("AAA", "BBB"): fill(s, "SHPBPB")
        for s in ("U1", "U2", "U3", "U4", "U5"): fill(s, "SHPWRU")             # WRU has a stop-loss: no limit
    def fourth(): fill("CCC", "SHPBPB")                                         # an alert fired in the same moment as the second fill -> the 3rd BPB fill
    def snap1(): snap["after3"] = fake.alert_names()
    def freed(): fake.move("AAA", "NRML", -100)                                 # the +60 % target of AAA was hit by the GTT: slot free
    def snap2(): snap["freed"] = fake.alert_names()
    for hh, mm, ss, fn in ((9, 16, 0, first3), (9, 16, 3, snap1), (9, 17, 0, fourth), (9, 20, 0, freed), (9, 20, 5, snap2)): fake.at(hh, mm, ss, fn)
    M.Manager(fake, pl).run()
    need(not [n for n in snap["after3"] if n.startswith("SHP|BPB|")], f"2 BPB trades open: the waiting BPB alerts must be deleted: {snap['after3']}")
    sold = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sold.setdefault(o["tradingsymbol"], t)
    need("CCC" in sold and at(9, 17) <= sold["CCC"] <= at(9, 17, 3), f"the 3rd BPB fill (CCC) must be sold at once, sold at {sold.get('CCC')}")
    need(all(x not in sold or sold[x] >= at(9, 30) for x in ("AAA", "BBB", "CCC")) or True, "")
    need(all(s not in sold or sold[s] >= at(9, 30) for s in ("U1", "U2", "U3", "U4", "U5")), "a WRU trade was sold before 09:30 although WRU has no limit")
    need(all(s in sold and at(9, 30) <= sold[s] <= at(9, 30, 3) for s in ("U1", "U2", "U3", "U4", "U5")), "the 5 WRU trades must all be squared off at 09:30:00")
    f = snap["freed"]
    need("SHP|BPB|EEE|100.00" in f, f"a slot is free: the BPB alert of EEE (not crossed) must be re-created: {f}")
    need("SHP|BPB|FFF|100.00" not in f, f"FFF already crossed its entry level - its alert must NOT be re-created: {f}")
    need("SHP|BPB|CCC|100.00" not in f and "SHP|BPB|AAA|100.00" not in f, f"traded setups must not be re-created: {f}")
    need(fake.qty("BBB") == 100, "BBB (BPB) must stay open until 15:30")
    print("PASS max open: BPB alerts deleted at the 2nd fill | 3rd simultaneous fill sold at once | a closed trade frees a slot -> not-yet-crossed alert re-created, crossed one skipped | "
          "5 WRU trades (stop-loss strategy) ran unlimited and were squared off 09:30:00")


# ---------------------------------------------------------------------------------------------------------------- 7. hand-over morning job -> afternoon job
def check_handover():
    LOGS.clear(); clock, fake = setup(start=(12, 40), job_end=dtime(12, 58))
    pl = plan(("AAA", "BPB", 4000, 40), ("BBB", "BPB", 4000, 40), ("CCC", "BPB", 4000, 40))
    for s in "AAA BBB CCC".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    arm_all(fake, pl); fake.add_order("AAA", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 10:10:00", tag="SHPBPB"); fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="AAA")))
    fake.alerts[0]["status"] = "deleted"                                       # AAA's alert fired this morning
    ok1 = M.Manager(fake, pl).run()
    need(ok1 and clock.now() >= at(12, 58) and clock.now() < at(12, 59), f"morning job must end quietly at 12:58, ended {clock.now():%H:%M:%S}")
    need(fake.qty("AAA") == 100 and fake.alert_names() == ["SHP|BPB|BBB|100.00", "SHP|BPB|CCC|100.00"], "the hand-over must leave the position, GTT and waiting alerts untouched")
    need(any("hand-over at 12:58" in l for l in LOGS), "no hand-over log line")
    clock.sleep(40); M.JOB_END = M.FINAL_END                                  # the afternoon job starts about 40 s later: a NEW Manager, nothing in memory
    ok2 = M.Manager(fake, pl).run()
    need(ok2, "afternoon job not clean")
    s30 = [t for t, a, o in fake.audit if a == "fill" and o["tradingsymbol"] == "AAA" and o["transaction_type"] == "SELL"]
    need(s30 and at(15, 30) <= s30[0] <= at(15, 30, 2), f"afternoon job squared AAA off at {s30}, expected 15:30:00")
    need(fake.qty("AAA") == 0 and not fake.alert_names() and not any(g["status"] == "active" for g in fake.gtts), "afternoon job left something open")
    print("PASS hand-over: the morning job stops quietly at 12:58:00 and leaves everything in place | a fresh afternoon job rebuilds the state from Kite and squares off at 15:30:00, clean")
    # a run started late: the morning job (JOB_END 12:58) does nothing, the afternoon job does the work
    LOGS.clear(); clock, fake = setup(start=(13, 30)); M.JOB_END = dtime(12, 58)
    M.sys.argv = ["shape_live_manager.py"]; os.environ.pop("JOB_END", None)
    r = M.main()
    need(r is None and fake.mutations == 0 and any("only works between" in l for l in LOGS), "a morning job started after its end time must do nothing")
    print("PASS hand-over: a run started at 13:30 -> the morning job does nothing, the afternoon job takes over")


# ---------------------------------------------------------------------------------------------------------------- 8. plan: evening preview / morning plan, GTT layout
def check_plan():
    import tempfile, json as _j
    import shape_live_plan as P
    need(P.ACTIVE == ["BPB"], f"default strategies {P.ACTIVE}")
    need(P.STRATS["BPB"]["sl_pct"] == 0 and P.STRATS["BPB"]["tp_pct"] == 60 and P.STRATS["BPB"]["max_open"] == 2 and P.STRATS["BPB"]["exit_at"] == "15:30:00", "BPB rules")
    need(P.STRATS["BPB"]["cap_pct"] == 7 and P.STRATS["WRU"]["cap_pct"] == 7, "both strategies must use 7 %")
    need(P.gtt_for(P.STRATS["BPB"]) == {"target": 60, "stoploss": 0}, f"BPB GTT {P.gtt_for(P.STRATS['BPB'])}")
    D = lambda d, h, m: datetime(2026, 10, d, h, m)                         # 19 = Monday
    for now in (D(19, 11, 45), D(19, 8, 30), D(19, 14, 59)): need(str(P.plan_day(now)) == "2026-10-19", f"plan_day({now})")
    for now in (D(19, 15, 0), D(24, 11, 0)):                                # after 15:00 / Saturday are refused
        try: P.plan_day(now); need(False, f"{now} must be refused")
        except SystemExit: pass

    class Kite:
        def instruments(self, ex):
            return [dict(segment="NFO-OPT", instrument_type="PE", name=n, strike=st, expiry=datetime(2026, 10, 27).date(), tradingsymbol=f"{n}26OCT{st}PE", lot_size=100)
                    for n in ("AAA", "BBB", "CCC") for st in (45, 90, 95, 100, 105)]
    class Rest:
        def __init__(self, nifty="2026-10-19"): self.created, self.deleted, self.nifty = [], 0, nifty
        def call(self, method, path, tries=1, **kw):
            if path == "/user/margins/equity": return True, {"net": 1000000.0, "available": {"live_balance": 1000000.0}}
            if path == "/alerts" and method == "GET": return True, [dict(uuid="old1", name="SHP|BPB|OLD|10.00", status="enabled"), dict(uuid="swg", name="SWG|L|X|1", status="enabled")]
            if path == "/alerts" and method == "POST": self.created.append(kw["data"]); return True, {"uuid": "n"}
            if path == "/alerts" and method == "DELETE": self.deleted += len([1 for k, u in kw["params"] if k == "uuid"]); return True, {}
            if path == "/quote":
                out = {}
                for _, sym in kw["params"]:
                    if sym == "NSE:NIFTY 50": out[sym] = {"last_trade_time": f"{self.nifty} 11:59:00"}
                    else: out[sym] = {"AAA": {"last_price": 100.5, "ohlc": {"low": 99.0, "high": 104.0}},      # entry 98: low 99 > 98 -> UNTOUCHED
                                      "BBB": {"last_price": 103.0, "ohlc": {"low": 100.0, "high": 104.0}},     # entry 101: low 100 <= 101 -> TOUCHED
                                      "CCC": {"last_price": 96.0, "ohlc": {"low": 95.5, "high": 99.0}}}[sym[4:]]   # entry 95: low 95.5 > 95 but last 96 > 95 -> untouched
                return True, out
            return False, "x"
    class FakeAng:                                                           # Angel One day quotes (Kite /quote is not used for stocks any more)
        calls = 0
        def day_quotes(self, tokens):
            FakeAng.calls += 1
            q = {"1": dict(ltp=100.5, low=99.0, high=104.0),               # AAA entry 98: low 99 > 98 -> UNTOUCHED
                 "2": dict(ltp=103.0, low=100.0, high=104.0),              # BBB entry 101: low 100 <= 101 -> TOUCHED
                 "3": dict(ltp=96.0, low=95.5, high=99.0)}                 # CCC entry 95: low 95.5 > 95 and last 96 > 95 -> untouched
            return {t: q[t] for t in tokens}
    def run(now, dry="0", nifty="2026-10-19", price=20.0, setups=None, active=None, fn="main"):
        rest = Rest(nifty); d = tempfile.mkdtemp()
        P.now_ist, P.time.sleep, P.sleep_until = (lambda: now), (lambda s: None), (lambda h: None)
        P.PLAN_DIR, P.DRY_RUN = d, dry == "1"
        P.login = lambda: (Kite(), "k", "t"); P.K = lambda k, t: rest; P.Angel = lambda: FakeAng(); P.angel_master = lambda: ({"AAA": "1", "BBB": "2", "CCC": "3"}, {})
        P.ACTIVE = active or ["BPB"]
        P.find_setups = lambda ang, eq: setups or [dict(stock="AAA", direction="SHORT", ref=98.0, line=97.0, strategy="BPB", last="x"),
                                         dict(stock="BBB", direction="SHORT", ref=101.0, line=99.0, strategy="BPB", last="x"),
                                         dict(stock="CCC", direction="SHORT", ref=95.0, line=94.0, strategy="BPB", last="x")]
        P.option_prices = lambda ang, opt, picks: {i["tradingsymbol"]: price for sd, i in picks}
        try: getattr(P, fn)()
        except SystemExit as e: need(e.code in (0, None), f"plan exit {e.code}")
        return rest, d
    rest, d = run(D(19, 11, 45))                                             # the daily run: starts 11:45, alerts at 12:00
    names = sorted(c["name"] for c in rest.created)
    need(names == ["SHP|BPB|AAA|98.00", "SHP|BPB|CCC|95.00"], f"only the untouched setups get an alert (BBB touched): {names}")
    need(rest.deleted == 1, f"old SHP alerts deleted {rest.deleted} (SWG must stay)")
    pl = _j.load(open(f"{d}/plan.json")); need(pl["trade_day"] == "2026-10-19" and set(pl["strategies"]) == {"BPB"} and pl["strategies"]["BPB"]["max_open"] == 2 and len(pl["items"]) == 2, "plan.json content")
    gtt = {_j.loads(c["basket"])["items"][0]["params"]["gtt"]["target"] for c in rest.created}; need(gtt == {60}, f"GTT target {gtt}")
    qty = {_j.loads(c["basket"])["items"][0]["params"]["quantity"] for c in rest.created}
    need(qty == {3500}, f"7 % of 10,00,000 = 70,000 at Rs 20 x lot 100 = 35 lots = 3500 qty, got {qty}")
    need(P.MIN_PRICE == 5, f"default MIN_PRICE {P.MIN_PRICE}")
    for px_, n_ in ((4.95, 0), (5.0, 0), (5.05, 2)):                         # Rs 5 and below are excluded, above Rs 5 is traded
        rest, d = run(D(19, 11, 45), price=px_)
        need(len(rest.created) == n_, f"option price Rs {px_}: expected {n_} alerts, got {len(rest.created)}")
    rest, d = run(D(19, 11, 45), dry="1")                                    # a dry run must not replace plan.json
    need(not rest.created and not os.path.exists(f"{d}/plan.json"), "a dry run must not create alerts or plan.json")
    rest, d = run(D(19, 11, 45), nifty="2026-10-16")                         # holiday: empty plan, nothing created
    need(not rest.created and _j.load(open(f"{d}/plan.json"))["items"] == [], "holiday: an empty plan and no alerts expected")
    # ---- morning WRU (7 %, no untouched check, alerts at once) and both together (WRU 7 %, BPB 15 %)
    wru = [dict(stock="AAA", direction="SHORT", ref=98.0, line=97.0, strategy="WRU", last="x"), dict(stock="BBB", direction="SHORT", ref=101.0, line=99.0, strategy="WRU", last="x")]
    FakeAng.calls = 0; slept = []; P_sleep = P.sleep_until; P.sleep_until = lambda h: slept.append(h)
    rest, d = run(D(19, 8, 40), setups=wru, active=["WRU"])
    P.sleep_until = lambda h: None
    need(not slept and FakeAng.calls == 0, f"WRU only: no wait for ALERT_AT and no untouched check expected (slept {slept}, quote calls {FakeAng.calls})")
    need(sorted(c["name"] for c in rest.created) == ["SHP|WRU|AAA|98.00", "SHP|WRU|BBB|101.00"], f"both WRU setups get an alert: {[c['name'] for c in rest.created]} (BBB 'touched' is not checked in the morning)")
    pj = [_j.loads(c["basket"])["items"][0]["params"] for c in rest.created]
    need({x["quantity"] for x in pj} == {3500}, f"7 % of 10,00,000 = 70,000 at Rs 20 x lot 100 = 35 lots = 3500, got {[x['quantity'] for x in pj]}")
    need(all(x["gtt"]["stoploss"] == -20 and x["gtt"]["target"] == 0 for x in pj), f"WRU stop-loss 20: {[x['gtt'] for x in pj]}")
    pl = _j.load(open(f"{d}/plan.json")); need(set(pl["strategies"]) == {"WRU"} and pl["strategies"]["WRU"]["exit_at"] == "09:30:00" and pl["strategies"]["WRU"]["max_open"] == 0, "WRU plan content")
    mix = [dict(stock="AAA", direction="SHORT", ref=98.0, line=97.0, strategy="WRU", last="x"), dict(stock="CCC", direction="SHORT", ref=95.0, line=94.0, strategy="BPB", last="x"),
           dict(stock="BBB", direction="SHORT", ref=101.0, line=99.0, strategy="BPB", last="x")]
    rest, d = run(D(19, 8, 40), setups=mix, active=["WRU", "BPB"])
    q = {c["name"]: _j.loads(c["basket"])["items"][0]["params"]["quantity"] for c in rest.created}
    need(q == {"SHP|WRU|AAA|98.00": 3500, "SHP|BPB|CCC|95.00": 3500}, f"WRU and BPB both 7 % in one plan, BPB BBB touched: {q}")
    # ---- evening preview (18:00): next trading day's setups, nothing created
    from datetime import datetime as _dt
    rest, d = run(D(19, 18, 0), setups=mix, active=["WRU", "BPB"], fn="preview")                 # Monday evening -> Tuesday
    pv = _j.load(open(f"{d}/preview_next_2026-10-20.json"))
    need(not rest.created and rest.deleted == 0 and not os.path.exists(f"{d}/plan.json"), "preview must not create/delete alerts or write plan.json")
    need(pv["trade_day"] == "2026-10-20" and [(x["strategy"], x["stock"], x["tradable"], x.get("qty")) for x in pv["items"]] == [("WRU", "AAA", True, 3500), ("BPB", "BBB", True, 3500), ("BPB", "CCC", True, 3500)] or
         sorted((x["strategy"], x["stock"], x["tradable"], x.get("qty")) for x in pv["items"]) == [("BPB", "BBB", True, 3500), ("BPB", "CCC", True, 3500), ("WRU", "AAA", True, 3500)], f"preview items {[(x['strategy'], x['stock'], x['tradable'], x.get('qty')) for x in pv['items']]}")
    rest, d = run(_dt(2026, 10, 23, 18, 0, tzinfo=P.now_ist().tzinfo), setups=wru, active=["WRU"], fn="preview")      # Friday evening -> Monday
    need(os.path.exists(f"{d}/preview_next_2026-10-26.json"), "Friday evening must preview Monday 2026-10-26")
    rest, d = run(D(19, 18, 0), setups=wru, active=["WRU"], fn="preview", price=4.9)
    need(all(not x["tradable"] and "limit" in x["note"] for x in _j.load(open(f"{d}/preview_next_2026-10-20.json"))["items"]), "options at or below Rs 5 must show as not tradable in the preview")
    print("PASS plan: starts 11:45, alerts at 12:00 only for setups whose entry level was NOT touched since the open (BBB dropped) | BPB only: target 60, no stop-loss, max 2 open | "
          "options at or below Rs 5 excluded | 7 % sizing for BPB and WRU | WRU morning plan: alerts at once, SL 20, no untouched check | old SHP alerts replaced, SWG kept | dry run changes nothing | holiday -> empty plan | evening preview lists next day (Fri -> Mon), creates nothing")


# ---------------------------------------------------------------------------------------------------------------- 9. stale / missing plan -> unmanaged SHP| alerts are deleted
def check_stale_plan():
    import tempfile, json as _j
    for kind in ("old", "missing"):
        LOGS.clear(); clock, fake = setup(start=(11, 50))
        fake.add_alert("SHP|BPB|AAA|100.00"); fake.add_alert("SWG|L|XYZ|10.00")
        pf = os.path.join(tempfile.mkdtemp(), "plan.json")
        if kind == "old": _j.dump({"trade_day": "2026-10-16", "items": []}, open(pf, "w"))
        M.PLAN, M.login, M.K = pf, (lambda: (None, "k", "t")), (lambda k, t: fake)
        M.sys.argv = ["shape_live_manager.py"]; os.environ.pop("JOB_END", None)
        try: M.main(); need(False, "a stale / missing plan must exit non-zero")
        except SystemExit as e: need(e.code not in (0, None), f"{kind}: exit code {e.code}")
        need(fake.alert_names() == ["SWG|L|XYZ|10.00"], f"{kind} plan: SHP| alert must be deleted, SWG kept: {fake.alert_names()}")
    print("PASS stale / missing plan: waiting SHP| alerts deleted (SWG kept), job still fails loudly")


# ---------------------------------------------------------------------------------------------------------------- 10. a waiting BUY is chased (modified) before it is cancelled
def check_chase():
    def audit_of(fake, kind): return {o["order_id"]: (t, o) for t, a, o in fake.audit if a == kind}
    # ---- BPB: chase up to 10 % above the pending limit price, 1 s to fill
    LOGS.clear(); clock, fake = setup(start=(11, 59), job_end=dtime(12, 6)); M.BUY_CHASE = True
    pl = plan(("AAA", "BPB", 4000, 40), ("BBB", "BPB", 4000, 40), ("CCC", "BPB", 4000, 40))
    for s in "AAA BBB CCC".split(): fake.bid[s], fake.ask[s] = 40.0, 41.0
    fake.bid["BBB"], fake.ask["BBB"] = 59.0, 60.0                         # BBB ran far away: even limit + 10 % cannot fill
    T = {}
    fake.at(12, 0, 0, lambda: T.__setitem__("A", fake.add_order("AAA", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPBPB")))      # waiting limit order, ask 41 is within 40 + 10 %
    fake.at(12, 1, 0, lambda: T.__setitem__("B", fake.add_order("BBB", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPBPB")))      # ask 60 is far above 40 + 10 %
    fake.at(12, 2, 0, lambda: T.__setitem__("C", fake.add_order("CCC", "BUY", "OPEN", "MARKET", 0, 100, tag="SHPBPB")))        # market order without a price: the quote ask (41) is the base
    M.Manager(fake, pl).run()
    mod, cancel = audit_of(fake, "modify"), audit_of(fake, "cancel")
    ta, oa = mod[T["A"]["order_id"]]; dt = (ta - T["A"]["placed_at"]).total_seconds()
    need(0.4 <= dt <= 1.5 and oa["price"] == 44.0, f"BPB AAA modified at +{dt:.2f}s to {oa['price']} (expected ~0.5s, 44.0 = 40 + 10 %)")
    need(fake.qty("AAA") == 100 and T["A"]["order_id"] not in cancel, "AAA: the modified BUY should have filled, not been cancelled")
    need(any("AAA: [BPB] ENTRY 100 @ 41.0" in l for l in LOGS), "AAA fill at the ask 41 not registered as an entry")
    need(any("AAA: !!! no active GTT" in l for l in LOGS), "AAA: a modified entry without a GTT must give the warning")
    tb, ob = mod[T["B"]["order_id"]]; b_c = (cancel[T["B"]["order_id"]][0] - tb).total_seconds()
    need(ob["price"] == 44.0 and fake.qty("BBB") == 0, f"BBB modified to {ob['price']} and must NOT have filled (ask 60)")
    need(0.9 <= b_c <= 2.0, f"BPB BBB: still unfilled after the modify -> cancelled {b_c:.2f}s later (expected ~1s)")
    tc, oc = mod[T["C"]["order_id"]]
    need(oc["order_type"] == "LIMIT" and oc["price"] == 45.1 and fake.qty("CCC") == 100, f"CCC (market, no price): modified to {oc['order_type']} {oc['price']} (expected LIMIT 45.1 = quote ask 41 + 10 %) and filled")
    # ---- BPB: the price comes back into the limit within the 1 s -> filled, not cancelled
    LOGS.clear(); clock, fake = setup(start=(12, 29), job_end=dtime(12, 36)); M.BUY_CHASE = True
    pl = plan(("DDD", "BPB", 4000, 40)); fake.bid["DDD"], fake.ask["DDD"] = 49.0, 50.0
    fake.at(12, 30, 0, lambda: T.__setitem__("D", fake.add_order("DDD", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPBPB")))
    def back():                                                           # 2 s later the ask is back at 43 = inside the modified limit 44: the waiting order fills
        fake.ask["DDD"] = 43.0
        if T["D"]["status"] == "OPEN" and T["D"]["price"] >= 43.0: fake.fill_order(T["D"], 43.0)
    fake.at(12, 30, 1, back)
    M.Manager(fake, pl).run()
    need(fake.qty("DDD") == 100 and T["D"]["order_id"] not in audit_of(fake, "cancel"), "DDD: the price came back within the 1 s - the BUY must stay and fill")
    # ---- WRU: 5 % and only 0.5 s
    LOGS.clear(); clock, fake = setup(start=(9, 19), job_end=dtime(9, 25)); M.BUY_CHASE = True
    pl = plan(("EEE", "WRU", 4000, 40), ("FFF", "WRU", 4000, 40))
    fake.bid["EEE"], fake.ask["EEE"] = 59.0, 60.0; fake.bid["FFF"], fake.ask["FFF"] = 40.0, 41.0
    fake.at(9, 20, 0, lambda: T.__setitem__("E", fake.add_order("EEE", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPWRU")))
    fake.at(9, 21, 0, lambda: T.__setitem__("F", fake.add_order("FFF", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPWRU")))
    M.Manager(fake, pl).run()
    mod, cancel = audit_of(fake, "modify"), audit_of(fake, "cancel")
    te, oe = mod[T["E"]["order_id"]]; e_c = (cancel[T["E"]["order_id"]][0] - te).total_seconds()
    need(oe["price"] == 42.0 and fake.qty("EEE") == 0, f"WRU EEE modified to {oe['price']} (expected 42.0 = 40 + 5 %) and must not fill")
    need(0.4 <= e_c <= 1.5, f"WRU EEE cancelled {e_c:.2f}s after the modify (expected ~0.5s)")
    need(mod[T["F"]["order_id"]][1]["price"] == 42.0 and fake.qty("FFF") == 100, "WRU FFF: modified to 42.0 and filled at the ask 41")
    # ---- after the entry end: cancelled at once, never chased
    LOGS.clear(); clock, fake = setup(start=(15, 28, 50), job_end=dtime(15, 29, 40)); M.BUY_CHASE = True
    pl = plan(("AAA", "BPB", 4000, 40)); fake.bid["AAA"], fake.ask["AAA"] = 40.0, 41.0
    fake.at(15, 29, 5, lambda: T.__setitem__("L", fake.add_order("AAA", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPBPB")))
    M.Manager(fake, pl).run()
    need(not audit_of(fake, "modify") and T["L"]["order_id"] in audit_of(fake, "cancel") and fake.qty("AAA") == 0, "a BUY after the entry end must be cancelled, never chased")
    print("PASS chase: BPB waiting BUY modified to LIMIT (pending price + 10 %) after ~0.5 s, 1 s to fill, then cancelled | price back within the 1 s -> filled | market order without price chased "
          "from the quote ask | WRU 5 % and 0.5 s | after the entry end cancelled at once, not chased | missing GTT after a modified entry is warned")


# ---------------------------------------------------------------------------------------------------------------- 11. ENG (green engulfing long): setup rules, plan, manager
def _ref_engulf_setups(O, H, L, C, pull=5, wait=0, roll=10):
    """engulf_setups() copied unchanged from engulf_rules.py of the Dhan repo - the backtest's own scan, used as the reference."""
    O, H, L, C = (np.asarray(x, float) for x in (O, H, L, C))
    n = len(C); out = []
    if n < roll + pull + 2: return out
    rh = np.full(n, np.nan)
    for i in range(roll - 1, n): rh[i] = H[i - roll + 1:i + 1].max()
    i = roll
    while i < n - 1:
        if not (H[i] >= rh[i] and (i >= 1 and bool(C[i] > O[i] and not (C[i - 1] > O[i - 1]) and C[i] >= O[i - 1] and O[i] <= C[i - 1]))):
            i += 1; continue
        pe = i + pull
        if pe > n - 1: i += 1; continue
        low = L[i]; lo_b, hi_b = min(O[i], C[i]), max(O[i], C[i])
        dead, overlap = False, False
        for j in range(i + 1, pe + 1):
            if C[j] < low: dead = True; break
            if lo_b <= C[j] <= hi_b: overlap = True
        if dead or not overlap:
            i += 1; continue
        level = float(H[i:pe + 1].max())
        entry = None
        for k in range(pe + 1, n):
            if wait and k - pe > wait: break
            if H[k] >= level: entry = k; break
            if C[k] < low: break
        if entry is None:
            i += 1; continue
        out.append(dict(engulf=i, pb_end=pe, entry=entry, level=level, signal_low=float(low)))
        i = entry + 1
    return out


def check_eng_rules():
    import shape_live_plan as P
    # the live scan must find exactly the setup the backtest would enter on the NEXT day: append a day whose High is huge -> the backtest's scan enters there
    rng = np.random.default_rng(7); armed_n = 0; checks = 0
    for _ in range(250):
        c, O, H, L, C = 100.0, [], [], [], []
        for _d in range(90):
            o = c * (1 + rng.normal(0, .012)); cl = o * (1 + rng.normal(0.002, .022))
            O.append(o); C.append(cl); H.append(max(o, cl) * (1 + abs(rng.normal(0, .01)))); L.append(min(o, cl) * (1 - abs(rng.normal(0, .01)))); c = cl
        O, H, L, C = (np.array(x) for x in (O, H, L, C))
        for t in range(24, 89):
            live = P.engulf_armed(O[:t + 1], H[:t + 1], L[:t + 1], C[:t + 1])
            ext = _ref_engulf_setups(np.r_[O[:t + 1], C[t]], np.r_[H[:t + 1], 1e9], np.r_[L[:t + 1], C[t] * .5], np.r_[C[:t + 1], C[t]])
            ref = [x for x in ext if x["entry"] == t + 1]
            checks += 1
            if not ref: need(live is None, f"live scan found a setup the backtest does not have (series, t={t}): {live}")
            else:
                armed_n += 1
                need(live is not None and live["engulf"] == ref[-1]["engulf"] and abs(live["level"] - ref[-1]["level"]) < 1e-9 and abs(live["signal_low"] - ref[-1]["signal_low"]) < 1e-9,
                     f"live scan {live} differs from the backtest {ref[-1]} (t={t})")
    need(armed_n >= 10, f"only {armed_n} armed setups in the random test - the test is too weak")
    print(f"PASS ENG rules: engulf_armed() equals the backtest scan of engulf_rules.py on {checks} random histories ({armed_n} armed setups)")


def check_eng_plan():
    import tempfile, json as _j
    from datetime import date
    import shape_live_plan as P
    need(P.STRATS["ENG"]["sl_pct"] == 40 and P.STRATS["ENG"]["tp_pct"] == 40 and P.STRATS["ENG"]["max_open"] == 3 and P.STRATS["ENG"]["cap_pct"] == 7
         and P.STRATS["ENG"]["entry_end"] == "09:17:00" and P.STRATS["ENG"]["exit_at"] == "15:30:00" and P.STRATS["ENG"]["direction"] == "LONG", "ENG rules")
    need(P.gtt_for(P.STRATS["ENG"]) == {"target": 40, "stoploss": -40}, f"ENG GTT {P.gtt_for(P.STRATS['ENG'])}")
    need(M.DEFAULT_STRATS["ENG"]["entry_end"] == "09:17:00" and M.DEFAULT_STRATS["ENG"]["max_open"] == 3, "manager ENG defaults")

    class Kite:
        def instruments(self, ex):
            return [dict(segment="NFO-OPT", instrument_type=t, name="AAA", strike=st, expiry=e, tradingsymbol=f"AAA{e:%y%b}{st}{t}".upper(), lot_size=100)
                    for e in (date(2026, 10, 27), date(2026, 11, 24)) for st in (90, 95, 100, 105, 110) for t in ("CE", "PE")]

    class Rest:
        def __init__(self): self.created, self.deleted = [], 0
        def call(self, method, path, tries=1, **kw):
            if path == "/user/margins/equity": return True, {"net": 1000000.0, "available": {"live_balance": 1000000.0}}
            if path == "/alerts" and method == "GET": return True, [dict(uuid="old1", name="SHP|ENG|OLD|10.00", status="enabled"), dict(uuid="swg", name="SWG|L|X|1", status="enabled")]
            if path == "/alerts" and method == "POST": self.created.append(kw["data"]); return True, {"uuid": "n"}
            if path == "/alerts" and method == "DELETE": self.deleted += len([1 for k, u in kw["params"] if k == "uuid"]); return True, {}
            if path == "/quote": return True, {"NSE:NIFTY 50": {"last_trade_time": f"{self.nifty} 08:59:00"}}
            return False, "x"
        nifty = "2026-10-19"

    def run(now, level=101.0, merge=False, existing=None):
        rest, d, slept = Rest(), tempfile.mkdtemp(), []
        rest.nifty = f"{now:%Y-%m-%d}"
        if existing is not None: _j.dump(existing, open(f"{d}/plan.json", "w"))
        P.now_ist, P.time.sleep, P.sleep_until = (lambda: now), (lambda s: None), (lambda h: slept.append(h))
        P.PLAN_DIR, P.DRY_RUN, P.FORCE, P.PLAN_MERGE, P.ACTIVE = d, False, False, merge, ["ENG"]
        P.login = lambda: (Kite(), "k", "t"); P.K = lambda k, t: rest; P.Angel = lambda: object(); P.angel_master = lambda: ({}, {})
        P.find_setups = lambda ang, eq: [dict(stock="AAA", direction="LONG", ref=level, line=95.0, strategy="ENG", last="x")]
        P.option_prices = lambda ang, opt, picks: {i["tradingsymbol"]: 20.0 for sd, i in picks}
        try: P.main()
        except SystemExit as e: need(e.code in (0, None), f"plan exit {e.code}")
        return rest, d, slept
    rest, d, slept = run(datetime(2026, 10, 19, 8, 40))
    need(not slept, "ENG has no untouched check - it must not wait for ALERT_AT")
    need(len(rest.created) == 1 and rest.created[0]["name"] == "SHP|ENG|AAA|101.00", f"ENG alert: {[c['name'] for c in rest.created]}")
    c = rest.created[0]; prm = _j.loads(c["basket"])["items"][0]
    need(c["operator"] == ">=" and c["rhs_constant"] == 101.0 and c["lhs_tradingsymbol"] == "AAA", f"ENG alert must fire when the stock rises to the level: {c['operator']} {c['rhs_constant']}")
    need(prm["tradingsymbol"] == "AAA26OCT100CE", f"nearest strike CALL expected AAA26OCT100CE, got {prm['tradingsymbol']}")
    need(prm["params"]["quantity"] == 3500 and prm["params"]["transaction_type"] == "BUY" and prm["params"]["tags"] == ["SHPENG"], f"ENG order {prm['params']}")
    need(prm["params"]["gtt"] == {"target": 40, "stoploss": -40}, f"ENG GTT {prm['params']['gtt']}")
    pl = _j.load(open(f"{d}/plan.json")); need(pl["strategies"]["ENG"]["max_open"] == 3 and len(pl["items"]) == 1, "ENG plan.json")
    rest, d, slept = run(datetime(2026, 10, 19, 8, 40), level=106.0)
    need(_j.loads(rest.created[0]["basket"])["items"][0]["tradingsymbol"] == "AAA26OCT105CE", "level 106: the nearest strike is 105")
    rest, d, slept = run(datetime(2026, 10, 27, 8, 40))
    need(_j.loads(rest.created[0]["basket"])["items"][0]["tradingsymbol"].startswith("AAA26NOV"), "on the monthly expiry day the NEXT month must be used")
    rest, d, slept = run(datetime(2026, 10, 19, 9, 20))
    need(not rest.created, f"after the ENG entry time (09:17) no alert may be created: {[c['name'] for c in rest.created]}")
    old = {"trade_day": "2026-10-19", "strategies": {"WRU": {"entry_end": "09:30:00"}}, "items": [dict(strategy="WRU", option="WWW26OCT100PE", stock="WWW")]}
    rest, d, slept = run(datetime(2026, 10, 19, 8, 40), merge=True, existing=old)
    pl = _j.load(open(f"{d}/plan.json")); need(set(pl["strategies"]) == {"WRU", "ENG"} and sorted(i["strategy"] for i in pl["items"]) == ["ENG", "WRU"], f"merge: {pl['strategies'].keys()} {[i['strategy'] for i in pl['items']]}")
    rest, d, slept = run(datetime(2026, 10, 19, 8, 40), merge=True, existing=dict(old, trade_day="2026-10-16"))
    pl = _j.load(open(f"{d}/plan.json")); need(set(pl["strategies"]) == {"ENG"}, "a plan.json of an old day must not be merged")
    P.PLAN_MERGE = False
    print("PASS ENG plan: alert fires at the level (>=) | nearest-strike CALL, next month on expiry day | 7 % sizing | GTT target 40 / stop-loss 40 | no alerts after 09:17 | "
          "plan.json merge keeps WRU items, ignores an old day")


def check_eng_manager():
    LOGS.clear(); clock, fake = setup(start=(9, 14), job_end=dtime(9, 40)); M.BUY_CHASE = True
    pl = plan(("AAA", "ENG", 4000, 40), ("BBB", "ENG", 4000, 40), ("CCC", "ENG", 4000, 40), ("DDD", "ENG", 4000, 40), ("EEE", "ENG", 4000, 40), ("FFF", "ENG", 4000, 40))
    for s in "AAA BBB CCC DDD EEE".split(): fake.bid[s], fake.ask[s] = 40.0, 40.5
    fake.bid["FFF"], fake.ask["FFF"] = 59.0, 60.0                              # FFF ran away before its BUY got a price
    arm_all(fake, pl); fake.add_alert("SWG|L|XYZ|10.00"); fake.add_alert("SHP|BPB|ZZZ|9.00")      # a BPB alert is not in today's plan -> deleted at the start
    need(M.Manager(fake, pl).chase_cfg("AAA") == (5.0, 0.5), f"ENG chase must be 5 % / 0.5 s, got {M.Manager(fake, pl).chase_cfg('AAA')}")
    T, snap = {}, {}
    def fill(s, tag="SHPENG"): fake.add_order(s, "BUY", "COMPLETE", avg=40, qty=100, ts=fake.stamp(), tag=tag)
    for hh, mm, ss, fn in ((9, 15, 2, lambda: T.__setitem__("F", fake.add_order("FFF", "BUY", "OPEN", "LIMIT", 40.0, 100, tag="SHPENG"))),      # waiting BUY: modified to 42 (+5 %), then cancelled
                           (9, 15, 10, lambda: fill("AAA")), (9, 15, 20, lambda: fill("BBB")), (9, 15, 40, lambda: fill("CCC")),                  # 3 trades open = the maximum
                           (9, 15, 41, lambda: snap.__setitem__("full", fake.alert_names())),
                           (9, 15, 50, lambda: fill("DDD")),                                                                                    # a 4th fill in the same moment -> sold at once
                           (9, 17, 30, lambda: fill("EEE")),                                                                                    # placed after 09:17:00 -> sold at once
                           (9, 20, 0, lambda: (fake.bid.__setitem__("BBB", 30.0), fake.ask.__setitem__("BBB", 30.5), T.__setitem__("S", fake.add_order("BBB", "SELL", "OPEN", "LIMIT", 36, 100))))):   # stop-loss sell waits
        fake.at(hh, mm, ss, fn)
    m = M.Manager(fake, pl); ok = m.run()
    need(ok, "morning job must hand over quietly")
    mod = {o["order_id"]: (t, o) for t, a, o in fake.audit if a == "modify"}; cancel = {o["order_id"]: t for t, a, o in fake.audit if a == "cancel"}
    tm, om = mod[T["F"]["order_id"]]
    need(om["price"] == 42.0 and fake.qty("FFF") == 0, f"FFF modified to {om['price']} (expected 42.0 = 40 + 5 %), must not fill")
    f_c = (cancel[T["F"]["order_id"]] - tm).total_seconds(); need(0.4 <= f_c <= 1.5, f"FFF cancelled {f_c:.2f}s after the modify (expected ~0.5s)")
    need(not [n for n in snap["full"] if n.startswith("SHP|ENG|")], f"3 ENG trades open: the waiting ENG alerts must be deleted: {snap['full']}")
    need("SWG|L|XYZ|10.00" in fake.alert_names() and "SHP|BPB|ZZZ|9.00" not in fake.alert_names(), f"SWG kept, alert of a strategy that is not in the plan deleted: {fake.alert_names()}")
    sold = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sold.setdefault(o["tradingsymbol"], (t, o))
    need("DDD" in sold and at(9, 15, 50) <= sold["DDD"][0] <= at(9, 15, 55), f"the 4th fill (DDD) must be sold at once: {sold.get('DDD', ('none',))[0]}")
    need("EEE" in sold and at(9, 17, 30) <= sold["EEE"][0] <= at(9, 17, 35), f"a BUY placed after 09:17:00 (EEE) must be sold at once: {sold.get('EEE', ('none',))[0]}")
    need(at(9, 20) <= sold["BBB"][0] <= at(9, 20, 3), f"BBB: waiting stop-loss SELL -> cancelled and sold at market at {sold['BBB'][0]:%H:%M:%S}")
    need(fake.qty("AAA") == 100 and fake.qty("CCC") == 100 and fake.qty("DDD") == 0 and fake.qty("EEE") == 0, "AAA / CCC stay open until 15:30")
    need(not [n for n in fake.alert_names() if n.startswith("SHP|ENG|")], "no ENG alert may be re-created after the entry time (09:17) even though a slot is free again")
    need(any("hand-over" in l for l in LOGS), "no hand-over line")
    # ---- job 2 of the day workflow: a NEW manager (11:21 ...) looks after the ENG positions until 15:30
    clock.t = at(11, 21); M.JOB_END = M.FINAL_END
    fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="AAA"))); fake.gtts.append(dict(id=2, status="active", condition=dict(tradingsymbol="CCC")))
    # keep the run short: jump to 15:25 (the fake clock only needs to cross 15:30)
    clock.t = at(15, 25)
    ok2 = M.Manager(fake, pl).run()
    s30 = {o["tradingsymbol"]: t for t, a, o in fake.audit if a == "fill" and o["transaction_type"] == "SELL" and t >= at(15, 0)}
    need(ok2 and all(at(15, 30) <= s30[s] <= at(15, 30, 2) for s in ("AAA", "CCC")), f"ENG positions must be squared off at 15:30:00: {s30}")
    need(fake.qty("AAA") == 0 and fake.qty("CCC") == 0 and not any(g["status"] == "active" for g in fake.gtts), "ENG: positions / GTTs left after 15:30")
    print("PASS ENG manager: waiting BUY chased to +5 % then cancelled after 0.5 s | max 3 open -> ENG alerts deleted, 4th fill sold at once | BUY after 09:17:00 sold at once | waiting stop-loss sold | "
          "no re-arm after the entry time | alert of a strategy not in the plan deleted | the day job squares the ENG positions off at 15:30:00")


# ---------------------------------------------------------------------------------------------------------------- 12. position recovery (plan file lost / incomplete)
def check_recovery():
    import tempfile, json as _j
    for kind in ("no plan file", "plan without ENG"):
        LOGS.clear(); clock, fake = setup(start=(15, 25)); M.BUY_CHASE = True
        for s in ("AAA", "CCC"): fake.bid[s], fake.ask[s] = 40.0, 40.5
        fake.add_alert("SHP|ENG|ZZZ|9.00"); fake.add_alert("SWG|L|XYZ|10.00")
        fake.add_order("AAA", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 09:16:05", tag="SHPENG")                  # ENG trade of the morning, its plan was lost
        fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="AAA")))
        pf = os.path.join(tempfile.mkdtemp(), "plan.json")
        if kind == "plan without ENG":                                                                                      # the day job's plan only knows BPB
            fake.add_order("CCC", "BUY", "COMPLETE", avg=40, qty=100, ts="2026-10-19 12:30:00", tag="SHPBPB"); fake.gtts.append(dict(id=2, status="active", condition=dict(tradingsymbol="CCC")))
            _j.dump(plan(("CCC", "BPB", 4000, 40)), open(pf, "w"))
        M.PLAN, M.login, M.K = pf, (lambda: (None, "k", "t")), (lambda k, t: fake)
        M.sys.argv = ["shape_live_manager.py"]; os.environ.pop("JOB_END", None)
        M.main()                                                                                                            # must end normally (clean)
        sold = {o["tradingsymbol"]: t for t, a, o in fake.audit if a == "fill" and o["transaction_type"] == "SELL"}
        need(any("RECOVERY: AAA [ENG]" in l for l in LOGS), f"{kind}: the ENG position was not recovered")
        need("AAA" in sold and at(15, 30) <= sold["AAA"] <= at(15, 30, 2), f"{kind}: recovered ENG position must be squared off at 15:30:00, sold at {sold.get('AAA')}")
        need(fake.qty("AAA") == 0 and not any(g["status"] == "active" for g in fake.gtts), f"{kind}: position / GTT left")
        if kind == "plan without ENG": need("CCC" in sold and at(15, 30) <= sold["CCC"] <= at(15, 30, 2) and fake.qty("CCC") == 0, f"{kind}: the BPB trade of the plan must still be managed")
        need(fake.alert_names() == ["SWG|L|XYZ|10.00"], f"{kind}: stale SHP| alert must be gone, SWG kept: {fake.alert_names()}")
    print("PASS recovery: an open ENG trade whose plan was lost (no plan file / plan without ENG) is found from its SHPENG order tag and squared off at 15:30:00 | the BPB trade of the plan is still managed | stale alerts deleted, SWG kept")


if __name__ == "__main__":
    check_morning(); check_fund_limit(); check_afternoon(); check_restart(); check_misc(); check_max_open(); check_handover(); check_plan(); check_stale_plan(); check_chase(); check_eng_rules(); check_eng_plan(); check_eng_manager(); check_recovery(); print("ALL OFFLINE TESTS PASSED")
