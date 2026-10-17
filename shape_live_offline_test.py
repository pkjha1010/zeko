"""
Offline test of shape_live_manager.py against a SIMULATED Zerodha (no login, no orders, no internet, runs in about a second).
    python shape_live_offline_test.py
It runs the whole morning on a fake clock (09:14 -> 09:30) and checks:
  - a BUY that price ran away from (waiting) is cancelled within ~0.5 s
  - a stop-loss SELL that waits (price ran past the limit) is cancelled and the position sold
  - a BUY waiting after 09:26 is cancelled at once; a fill placed after 09:26 is sold at once
  - waiting ATO alerts (SHP|) are deleted at 09:26, other alerts (SWG|) are left alone
  - every position is squared off at 09:30:00, also when MARKET orders keep waiting (aggressive LIMIT takes over)
  - the 09:30 sweep clears a position the manager never saw, leftover waiting orders and leftover stop-loss GTTs
  - positions / orders of other strategies are NOT touched (SWEEP_ALL=0) and ARE cleared with SWEEP_ALL=1
  - DRY_RUN sends nothing
"""
import os, sys, types, threading
from datetime import datetime, timedelta
for _m in ("kiteconnect", "pyotp"):                       # only needed for the real login; stubbed when not installed
    try: __import__(_m)
    except ImportError:
        mod = types.ModuleType(_m); sys.modules[_m] = mod
        if _m == "kiteconnect": mod.KiteConnect = object
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shape_live_manager as M

DAY = datetime(2026, 10, 19)                               # a Monday


class Clock:
    def __init__(self, h, m, s=0): self.t = datetime(2026, 10, 19, h, m, s); self.hooks = []
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
        self.bid, self.ask, self.market_stuck, self.n, self.mutations = {}, {}, set(), 0, 0
        clock.hooks.append(self.fire)

    def at(self, hh, mm, ss, fn): self.events.append([datetime(2026, 10, 19, hh, mm, ss), fn, False])
    def fire(self):
        for ev in self.events:
            if not ev[2] and ev[0] <= self.c.t: ev[2] = True; ev[1]()
    def stamp(self): return self.c.t.strftime("%Y-%m-%d %H:%M:%S")

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
        if path == "/portfolio/positions": return True, {"net": [dict(tradingsymbol=s, product=p, exchange="NFO", quantity=q) for (s, p), q in self.pos.items()], "day": []}
        if path == "/gtt/triggers" and method == "GET": return True, [dict(g) for g in self.gtts]
        if path.startswith("/gtt/triggers/") and method == "DELETE":
            for g in self.gtts:
                if str(g["id"]) == path.rsplit("/", 1)[1]: g["status"] = "deleted"
            return True, {}
        if path == "/alerts" and method == "GET": return True, [dict(a) for a in self.alerts if a["status"] == "enabled"]
        if path == "/alerts" and method == "DELETE":
            ids = {u for k, u in kw["params"] if k == "uuid"}
            for a in self.alerts:
                if a["uuid"] in ids: a["status"] = "deleted"
            return True, {}
        if path == "/quote":
            sym = kw["params"][0][1]
            if sym == "NSE:NIFTY 50": return True, {sym: {"last_trade_time": self.stamp()}}
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


def setup(sweep_all=False, dry=False):
    clock = Clock(9, 14); fake = Fake(clock)
    M.time, M.now_ist, M.SWEEP_ALL, M.DRY_RUN = clock, clock.now, sweep_all, dry
    M.threading = types.SimpleNamespace(RLock=threading.RLock, Thread=SyncThread)
    M.log = lambda m: LOGS.append(f"[{clock.now():%H:%M:%S}] {m}")
    return clock, fake


def plan(*syms): return {"trade_day": "2026-10-19", "items": [dict(option=s, qty=100, close_price=50.0, stock=s[:3], direction="LONG") for s in syms]}


def main_scenario():
    clock, fake = setup()
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
    for s in syms + ["ZZZ"]: fake.bid[s], fake.ask[s] = 50.0, 50.5
    fake.alerts = [dict(uuid="u1", name="SHP|L|AAA|100.00", status="enabled"), dict(uuid="u2", name="SHP|BS|BBB|90.00", status="enabled"), dict(uuid="u3", name="SWG|L|XYZ|10.00", status="enabled")]
    fake.market_stuck = {"DDD"}; fake.bid["DDD"], fake.ask["DDD"] = 40.0, 40.5
    fake.move("EEE", "NRML", 100)                                    # a position of a plan option whose BUY the manager never saw
    fake.move("ZZZ", "NRML", 50)                                     # another strategy's position
    fake.add_order("ZZZ", "SELL", "OPEN", "LIMIT", 70, 50)           # ... and its waiting order
    T = {}
    def A(): T["A"] = fake.add_order("AAA", "BUY", "OPEN", "MARKET", qty=100)                         # price ran away, BUY waits
    def B1():
        fake.add_order("BBB", "BUY", "COMPLETE", avg=100, qty=100, ts=fake.stamp()); fake.gtts.append(dict(id=1, status="active", condition=dict(tradingsymbol="BBB")))
    def B2(): fake.bid["BBB"], fake.ask["BBB"] = 80.0, 80.5; fake.gtts[0]["status"] = "triggered"; T["B"] = fake.add_order("BBB", "SELL", "OPEN", "LIMIT", 90, 100)     # stop-loss limit order waits
    def C(): fake.add_order("CCC", "BUY", "COMPLETE", avg=60, qty=200, ts=fake.stamp()); fake.gtts.append(dict(id=2, status="active", condition=dict(tradingsymbol="CCC")))
    def D(): fake.add_order("DDD", "BUY", "COMPLETE", avg=45, qty=100, ts=fake.stamp())
    def F(): fake.add_order("FFF", "BUY", "COMPLETE", avg=55, qty=100, ts="2026-10-19 09:25:30")
    def G(): T["G"] = fake.add_order("GGG", "BUY", "OPEN", "MARKET", qty=100)                          # waits after 09:26
    def H(): fake.add_order("HHH", "BUY", "COMPLETE", avg=55, qty=100, ts="2026-10-19 09:26:30")      # filled for an order placed after 09:26
    def E(): T["E"] = fake.add_order("EEE", "SELL", "OPEN", "LIMIT", 99, 100, tag="SHPOLD"); fake.gtts.append(dict(id=3, status="active", condition=dict(tradingsymbol="EEE")))   # leftover waiting sell (our own tag, so only the sweep deals with it) + GTT
    def E2(): T["E2"] = fake.add_order("EEE", "BUY", "OPEN", "LIMIT", 20, 100)
    for hh, mm, ss, fn in ((9, 16, 0, A), (9, 17, 0, B1), (9, 18, 0, B2), (9, 20, 0, C), (9, 24, 0, D), (9, 25, 30, F), (9, 27, 0, G), (9, 26, 40, H), (9, 29, 30, E), (9, 29, 50, E2)): fake.at(hh, mm, ss, fn)
    m = M.Manager(fake, plan(*syms)); ok = m.run()
    return clock, fake, T, ok


def check_main():
    clock, fake, T, ok = main_scenario()
    dump = "\n".join(LOGS[-60:])
    def fails(msg): raise AssertionError(msg + "\n--- last log lines ---\n" + dump)
    if not ok: fails("run() reported NOT clean")
    for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"):
        if fake.qty(s) != 0: fails(f"{s} still has position {fake.qty(s)}")
        if fake.open_orders(s): fails(f"{s} still has waiting orders")
    if fake.qty("ZZZ") != 50 or len(fake.open_orders("ZZZ")) != 1: fails("another strategy's position/order was touched with SWEEP_ALL=0")
    if [a["status"] for a in fake.alerts] != ["deleted", "deleted", "enabled"]: fails(f"alerts: {[a['status'] for a in fake.alerts]}")
    if any(g["status"] == "active" for g in fake.gtts): fails("an active stop-loss GTT is left")
    cancel_t = {o["order_id"]: t for t, a, o in fake.audit if a == "cancel"}
    a_dt = (cancel_t[T["A"]["order_id"]] - T["A"]["placed_at"]).total_seconds()
    if not 0.4 <= a_dt <= 1.5: fails(f"waiting BUY cancelled after {a_dt:.2f}s (expected ~0.5s)")
    g_dt = (cancel_t[T["G"]["order_id"]] - T["G"]["placed_at"]).total_seconds()
    if g_dt > 1.0: fails(f"BUY waiting after 09:26 cancelled after {g_dt:.2f}s (expected at once)")
    b_dt = (cancel_t[T["B"]["order_id"]] - T["B"]["placed_at"]).total_seconds()
    if not 0.4 <= b_dt <= 1.5: fails(f"waiting stop-loss SELL cancelled after {b_dt:.2f}s")
    sells = {}
    for t, a, o in fake.audit:
        if a == "fill" and o["transaction_type"] == "SELL": sells.setdefault(o["tradingsymbol"], []).append((t, o))
    tb = sells["BBB"][0][0]
    if not datetime(2026, 10, 19, 9, 18) <= tb <= datetime(2026, 10, 19, 9, 18, 3): fails(f"BBB stop-loss position sold at {tb:%H:%M:%S.%f} (should be right after 09:18:00)")
    for s in ("CCC", "FFF", "DDD", "EEE"):
        t0 = sells[s][0][0]
        if not datetime(2026, 10, 19, 9, 30) <= t0 <= datetime(2026, 10, 19, 9, 30, 12): fails(f"{s} squared off at {t0:%H:%M:%S}, expected 09:30:00-09:30:12")
    if sells["CCC"][0][0] > datetime(2026, 10, 19, 9, 30, 1): fails(f"CCC (normal exit) sold at {sells['CCC'][0][0]:%H:%M:%S.%f}, expected right at 09:30:00")
    if sells["DDD"][0][1]["order_type"] != "LIMIT": fails("DDD (market orders keep waiting) should have been closed by the aggressive LIMIT order")
    th = sells["HHH"][0][0]
    if not datetime(2026, 10, 19, 9, 26, 40) <= th <= datetime(2026, 10, 19, 9, 26, 43): fails(f"late fill HHH sold at {th:%H:%M:%S}, expected at once")
    if clock.now() > datetime(2026, 10, 19, 9, 31): fails(f"job needed until {clock.now():%H:%M:%S} - the sweep should finish within seconds of 09:30")
    if not any("SWEEP CLEAN" in l for l in LOGS): fails("no 'SWEEP CLEAN' line")
    print("PASS main scenario: stuck BUY cancelled in %.1fs | stop-loss SELL cancelled in %.1fs and position sold | late BUY cancelled at once, late fill sold at once | alerts deleted 09:26 (SWG kept) |"
          " all squared off at 09:30:00 (DDD via aggressive limit) | untracked position, leftover orders and GTT cleared by the sweep | job clean at %s" % (a_dt, b_dt, f"{clock.now():%H:%M:%S}"))


def check_sweep_all_and_dry():
    LOGS.clear(); clock, fake = setup(sweep_all=True)
    fake.bid["ZZZ"], fake.ask["ZZZ"] = 30.0, 30.5; fake.bid["AAA"], fake.ask["AAA"] = 30.0, 30.5
    fake.move("ZZZ", "NRML", 50); fake.add_order("ZZZ", "SELL", "OPEN", "LIMIT", 70, 50); fake.add_order("QQQ", "BUY", "OPEN", "LIMIT", 5, 75)
    ok = M.Manager(fake, plan("AAA")).run()
    assert ok and fake.qty("ZZZ") == 0 and not fake.open_orders(), "\n".join(LOGS[-30:])
    print("PASS SWEEP_ALL=1: another strategy's NFO position and waiting orders are cleared too")
    LOGS.clear(); clock, fake = setup(dry=True)
    fake.alerts = [dict(uuid="u1", name="SHP|L|AAA|100.00", status="enabled")]; fake.add_order("AAA", "BUY", "OPEN", "MARKET", qty=100)
    M.Manager(fake, plan("AAA")).run()
    assert fake.mutations == 0, f"DRY_RUN sent {fake.mutations} changing calls"
    print("PASS DRY_RUN: nothing sent")


if __name__ == "__main__":
    check_main(); check_sweep_all_and_dry(); print("ALL OFFLINE TESTS PASSED")
