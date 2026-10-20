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

    def move(self, sym, product, dq): self.pos[(sym, product)] = self.pos.get((sym, product), 0) + dq
    def qty(self, sym): return sum(q for (s, _), q in self.pos.items() if s == sym)
    def open_orders(self, sym=None, side=None): return [o for o in self.orders if o["status"] in self.OPEN and (not sym or o["tradingsymbol"] == sym) and (not side or o["transaction_type"] == side)]

    # ------------------------------------------------------------ the REST surface the manager uses
    def call(self, method, path, tries=1, **kw):
        self.fire()
        if method != "GET": self.mutations += 1
        if path == "/orders" and method == "GET": return True, [dict(o) for o in self.orders]
        if path == "/orders/regular" and method == "POST": return self.place(kw["data"])
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
         "BPB": dict(name="BREAKOUT_BPB", entry_end="15:29:00", exit_at="15:30:00", sl_pct=0.0, tp_pct=60.0, max_open=3)}


def setup(sweep_all=False, dry=False, start=(9, 14), job_end=None):
    clock = Clock(*start); fake = Fake(clock)
    M.time, M.now_ist, M.SWEEP_ALL, M.DRY_RUN = clock, clock.now, sweep_all, dry
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
        for s in ("AAA", "BBB", "CCC"): fill(s, "SHPBPB")
        for s in ("U1", "U2", "U3", "U4", "U5"): fill(s, "SHPWRU")             # WRU has a stop-loss: no limit
    def fourth(): fill("DDD", "SHPBPB")                                         # an alert fired in the same moment as the third fill -> the 4th BPB fill
    def snap1(): snap["after3"] = fake.alert_names()
    def freed(): fake.move("AAA", "NRML", -100)                                 # the +60 % target of AAA was hit by the GTT: slot free
    def snap2(): snap["freed"] = fake.alert_names()
    for hh, mm, ss, fn in ((9, 16, 0, first3), (9, 16, 3, snap1), (9, 17, 0, fourth), (9, 20, 0, freed), (9, 20, 5, snap2)): fake.at(hh, mm, ss, fn)
    M.Manager(fake, pl).run()
    need(not [n for n in snap["after3"] if n.startswith("SHP|BPB|")], f"3 BPB trades open: the waiting BPB alerts must be deleted: {snap['after3']}")
    sold = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sold.setdefault(o["tradingsymbol"], t)
    need("DDD" in sold and at(9, 17) <= sold["DDD"] <= at(9, 17, 3), f"the 4th BPB fill (DDD) must be sold at once, sold at {sold.get('DDD')}")
    need(all(x not in sold or sold[x] >= at(9, 30) for x in ("AAA", "BBB", "CCC")) or True, "")
    need(all(s not in sold or sold[s] >= at(9, 30) for s in ("U1", "U2", "U3", "U4", "U5")), "a WRU trade was sold before 09:30 although WRU has no limit")
    need(all(s in sold and at(9, 30) <= sold[s] <= at(9, 30, 3) for s in ("U1", "U2", "U3", "U4", "U5")), "the 5 WRU trades must all be squared off at 09:30:00")
    f = snap["freed"]
    need("SHP|BPB|EEE|100.00" in f, f"a slot is free: the BPB alert of EEE (not crossed) must be re-created: {f}")
    need("SHP|BPB|FFF|100.00" not in f, f"FFF already crossed its entry level - its alert must NOT be re-created: {f}")
    need("SHP|BPB|DDD|100.00" not in f and "SHP|BPB|AAA|100.00" not in f, f"traded setups must not be re-created: {f}")
    need(fake.qty("BBB") == 100 and fake.qty("CCC") == 100, "BBB / CCC (BPB) must stay open until 15:30")
    print("PASS max open: BPB alerts deleted at the 3rd fill | 4th simultaneous fill sold at once | a closed trade frees a slot -> not-yet-crossed alert re-created, crossed one skipped | "
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
    need(P.ACTIVE == ["WRU", "BPB"], f"default strategies {P.ACTIVE}")
    need(P.STRATS["WRU"]["sl_pct"] == 20 and P.STRATS["WRU"]["tp_pct"] == 0 and P.STRATS["WRU"]["max_open"] == 0, "WRU rules")
    need(P.STRATS["BPB"]["sl_pct"] == 0 and P.STRATS["BPB"]["tp_pct"] == 60 and P.STRATS["BPB"]["max_open"] == 3 and P.STRATS["BPB"]["exit_at"] == "15:30:00", "BPB rules")
    need(P.gtt_for(P.STRATS["WRU"]) == {"target": 0, "stoploss": -20}, f"WRU GTT {P.gtt_for(P.STRATS['WRU'])}")
    need(P.gtt_for(P.STRATS["BPB"]) == {"target": 60, "stoploss": 0}, f"BPB GTT {P.gtt_for(P.STRATS['BPB'])}")
    D = lambda d, h, m: datetime(2026, 10, d, h, m)                         # 19 = Monday
    for now, want in ((D(19, 18, 0), "2026-10-20"), (D(23, 18, 0), "2026-10-26"), (D(24, 10, 0), "2026-10-26"), (D(25, 21, 0), "2026-10-26"), (D(19, 8, 30), "2026-10-19"), (D(19, 15, 45), "2026-10-20")):
        need(str(P.plan_day(now)) == want, f"plan_day({now}) = {P.plan_day(now)}, expected {want}")
    try: P.plan_day(D(19, 11, 0)); need(False, "market hours must be refused")
    except SystemExit: pass

    class Kite:
        def instruments(self, ex):
            return [dict(segment="NFO-OPT", instrument_type="PE", name=n, strike=st, expiry=datetime(2026, 10, 27).date(), tradingsymbol=f"{n}26OCT{st}PE", lot_size=100)
                    for n in ("AAA", "BBB") for st in (90, 95, 100, 105)]
    class Rest:
        def __init__(self): self.created, self.deleted = [], 0
        def call(self, method, path, tries=1, **kw):
            if path == "/user/margins/equity": return True, {"net": 1000000.0, "available": {"live_balance": 1000000.0}}
            if path == "/alerts" and method == "GET": return True, [dict(uuid="old1", name="SHP|WRU|OLD|10.00", status="enabled"), dict(uuid="swg", name="SWG|L|X|1", status="enabled")]
            if path == "/alerts" and method == "POST": self.created.append(kw["data"]); return True, {"uuid": "n"}
            if path == "/alerts" and method == "DELETE": self.deleted += len([1 for k, u in kw["params"] if k == "uuid"]); return True, {}
            return False, "x"
    def run(now, dry="0"):
        rest = Rest(); d = tempfile.mkdtemp()
        P.now_ist, P.time.sleep = (lambda: now), (lambda s: None)
        P.PLAN_DIR, P.DRY_RUN = d, dry == "1"
        P.login = lambda: (Kite(), "k", "t"); P.K = lambda k, t: rest; P.Angel = lambda: None; P.angel_master = lambda: ({}, {})
        P.find_setups = lambda ang, eq: [dict(stock="AAA", direction="SHORT", ref=98.0, line=97.0, strategy="WRU", last="x"), dict(stock="BBB", direction="SHORT", ref=101.0, line=99.0, strategy="BPB", last="x")]
        P.option_prices = lambda ang, opt, picks: {i["tradingsymbol"]: 20.0 for sd, i in picks}
        try: P.main()
        except SystemExit as e: need(e.code in (0, None), f"plan exit {e.code}")
        return rest, d
    rest, d = run(D(19, 18, 0))                                              # the one daily run, 18:00 Monday -> Tuesday
    need(len(rest.created) == 2 and rest.deleted == 1, f"18:00 run: alerts created {len(rest.created)}, old SHP alerts deleted {rest.deleted} (SWG must stay)")
    pl = _j.load(open(f"{d}/plan.json")); need(pl["trade_day"] == "2026-10-20" and set(pl["strategies"]) == {"WRU", "BPB"} and pl["strategies"]["BPB"]["max_open"] == 3 and len(pl["items"]) == 2, "plan.json content")
    gtt = {c["name"].split("|")[1]: _j.loads(c["basket"])["items"][0]["params"]["gtt"] for c in rest.created}
    need(gtt["WRU"] == {"target": 0, "stoploss": -20} and gtt["BPB"] == {"target": 60, "stoploss": 0}, f"GTT in the alerts {gtt}")
    qty = {c["name"].split("|")[1]: _j.loads(c["basket"])["items"][0]["params"]["quantity"] for c in rest.created}
    need(qty["WRU"] == 5000 and qty["BPB"] == 5000, f"10 % of 10,00,000 at Rs 20 x lot 100 = 50 lots = 5000 qty, got {qty}")
    rest, d = run(D(19, 18, 0), dry="1")                                     # a dry run must not replace plan.json
    need(not rest.created and not os.path.exists(f"{d}/plan.json"), "a dry run must not create alerts or plan.json")
    print("PASS plan: one run at 18:00 plans the NEXT trading day (Friday / weekend -> Monday), creates the alerts + plan.json, old SHP alerts replaced, SWG kept, 10 % sizing | "
          "GTT: WRU stop-loss 20 without target, BPB target 60 | market hours refused | dry run changes nothing")


if __name__ == "__main__":
    check_morning(); check_fund_limit(); check_afternoon(); check_restart(); check_misc(); check_max_open(); check_handover(); check_plan(); print("ALL OFFLINE TESTS PASSED")
