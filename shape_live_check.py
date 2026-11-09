"""
SHAPELIVE CHECK  -  a safe end-to-end test you can run while the market is open.  It trades NOTHING for real: the test order is a LIMIT BUY priced far BELOW the market (it cannot fill),
it is modified once and then CANCELLED, and the script checks that no order or position is left behind.

Put it in the zeko repo next to shape_live_manager.py and shape_live_plan.py (it imports both) and run:   python shape_live_check.py
Same secrets as the other workflows (KITE_API_KEY, KITE_API_SECRET, ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_KEY, ORDER_PROXY if you use one).

What it checks (PASS / FAIL for each):
  1. Kite login, funds, positions, order book, GTT list, alert list (all read-only).
  2. Kite quote data: depth (bid / ask), last price, open interest, volume of the test option - this is what the new chase and the liquidity filter need.
  3. The liquidity filter of shape_live_plan.py on that option (printed with the reason).
  4. The order flow: place the far-below limit BUY -> order reaches OPEN -> modify its price -> modify confirmed -> cancel -> order shows CANCELLED -> nothing left open.
  5. What the new manager chase would send for this option right now (computed only, NOT sent).

Test option: env TEST_OPTION=AUBANK26NOV920PE, or TEST_STOCK (default SBIN) + TEST_TYPE (PE / CE, default PE) = the ATM option of the nearest monthly expiry.
Env TEST_ORDER=0 = skip step 4 (no order at all, reads and quotes only).   Test orders carry the tag SHPTEST.   Exit code 1 if anything failed.
"""
import os, sys, time
from datetime import time as dtime
import shape_live_manager as M
import shape_live_plan as P

log, tick = M.log, M.tick
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    log(("PASS  " if ok else "FAIL  ") + name + (f" - {detail}" if detail else ""))
    return bool(ok)


def order_row(k, oid):
    ok, rows = k.call("GET", "/orders", tries=2)
    if not ok: return None
    return next((o for o in rows or [] if str(o.get("order_id")) == str(oid)), None)


def wait_for(k, oid, test, secs=6.0):
    """Poll the order book every 0.3 s until test(order row) is true.  -> (row, seconds it took) or (last row, None)."""
    t0, row = time.time(), None
    while time.time() - t0 < secs:
        row = order_row(k, oid)
        if row and test(row): return row, round(time.time() - t0, 2)
        time.sleep(0.3)
    return row, None


def pick_option(kite, k):
    inst = P.kite_instruments(kite)
    forced = (os.environ.get("TEST_OPTION") or "").strip()
    if forced:
        i = next((x for x in inst if x["tradingsymbol"] == forced), None)
        if not i: sys.exit(f"TEST_OPTION {forced} not found in the Kite NFO instruments")
        return i
    stock, typ = (os.environ.get("TEST_STOCK") or "SBIN").strip().upper(), (os.environ.get("TEST_TYPE") or "PE").strip().upper()
    ok, d = k.call("GET", "/quote", tries=2, params=[("i", f"NSE:{stock}")])
    spot = float(((d or {}).get(f"NSE:{stock}") or {}).get("last_price") or 0) if ok else 0
    if spot <= 0: sys.exit(f"cannot read the price of {stock} from Kite - quote data missing?")
    lst = [x for x in inst if x.get("segment") == "NFO-OPT" and x["name"] == stock and x["instrument_type"] == typ]
    exps = [e for e in P.monthly_expiries([x["expiry"] for x in lst]) if e > M.now_ist().date()]
    if not exps: sys.exit(f"no monthly expiry found for {stock}")
    cands = [x for x in lst if x["expiry"] == exps[0]]
    return min(cands, key=lambda x: (abs(float(x["strike"]) - spot), float(x["strike"])))


def main():
    t = M.now_ist().time()
    live = dtime(9, 15) <= t < dtime(15, 30)
    log(f"SHAPELIVE CHECK at {t:%H:%M:%S} IST - market {'OPEN' if live else 'CLOSED (no order test, depth may be empty)'}")
    kite, key, tok = M.login(); k = M.K(key, tok)
    check("Kite login", True)

    # 1. read-only account calls
    f = P.funds_available(k); check("funds (/user/margins)", f is not None, f"available Rs {f:,.0f}" if f is not None else "cannot read")
    ok, pos = k.call("GET", "/portfolio/positions", tries=2); check("positions", ok and isinstance(pos, dict), f"{sum(1 for p in (pos or {}).get('net', []) if int(p.get('quantity') or 0))} open" if ok else str(pos))
    ok, rows = k.call("GET", "/orders", tries=2); check("order book", ok, f"{len(rows or [])} orders today" if ok else str(rows))
    ok, g = k.call("GET", "/gtt/triggers", tries=2); check("GTT list", ok, f"{len(g or [])} GTTs" if ok else str(g))
    ok, a = k.call("GET", "/alerts", tries=2, params={"status": "enabled", "page_size": 100}); check("alerts list (ATO)", ok, f"{len(a or [])} enabled alerts" if ok else str(a))

    # 2. the test option and its quote
    opt = pick_option(kite, k); sym, lot = opt["tradingsymbol"], int(opt["lot_size"])
    log(f"test option {sym} (lot {lot}, expiry {opt['expiry']}, strike {opt['strike']})")
    ok, d = k.call("GET", "/quote", tries=2, params=[("i", f"NFO:{sym}")])
    q = ((d or {}).get(f"NFO:{sym}") or {}) if ok else {}
    dp = q.get("depth") or {}
    bid = float((((dp.get("buy")) or [{}])[0]).get("price") or 0); ask = float((((dp.get("sell")) or [{}])[0]).get("price") or 0)
    ltp = float(q.get("last_price") or 0); oi = float(q.get("oi") or 0); vol = float(q.get("volume") or q.get("volume_traded") or 0)
    check("quote data (depth / LTP / OI / volume)", ok and ltp > 0, f"bid {bid} ask {ask} ltp {ltp} | OI {oi:,.0f} units = {oi / lot:,.0f} lots | volume {vol:,.0f}" if ok else str(d))
    if live: check("market depth has a bid and an ask", bid > 0 and ask > 0, f"bid {bid} ask {ask}")

    # 3. the liquidity filter on this option
    good, why = P.liquidity(q, lot, ltp <= P.MIN_PRICE, live, lot)
    log(f"liquidity filter on {sym}: {'WOULD PASS' if good else 'WOULD REJECT'} - {why}")
    log(f"(filter settings: Rs {P.LOW_MIN_PRICE:g} floor, OI >= {P.LIQ_MIN_OI_LOTS:g} lots, spread <= {P.LIQ_MAX_SPREAD_TICKS} ticks or {P.LIQ_MAX_SPREAD_PCT:g} %, ask depth >= {P.LIQ_DEPTH_MULT:g} x qty)")

    # 5 (computed first, nothing is sent): what the manager chase would do
    if ask > 0:
        n = 1; base = ltp or ask; cap = tick(base * (1 + max(M.CHASE_CAP_PCT, 5) / 100)); new = min(tick(ask + 0.05 * M.CHASE_TICKS * n), cap)
        log(f"chase preview (NOT sent): round {n} would modify an unfilled BUY to LIMIT {new} = best ask {ask} + {M.CHASE_TICKS * n} ticks (cap {cap} = {max(M.CHASE_CAP_PCT, 5):g} % over {base})")

    # 4. the order flow with a limit far below the market (cannot fill)
    if not live or (os.environ.get("TEST_ORDER") or "1") == "0":
        log("order test skipped (" + ("market closed" if not live else "TEST_ORDER=0") + ")")
    else:
        ref = bid or ltp
        price = max(0.05, tick(ref * 0.75)); new_price = max(price + 0.05, tick(price * 1.05))
        if ref <= 0 or new_price >= ref: log("order test skipped: no usable bid / price (the option is too cheap or has no quote)")
        else:
            log(f"order test: LIMIT BUY {lot} x {sym} @ {price} (market bid {bid}, ltp {ltp}) - far below the market, it cannot fill")
            t0 = time.time()
            ok, d = k.call("POST", "/orders/regular", tries=1, data=dict(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY", quantity=lot,
                                                                         product=M.PRODUCT, order_type="LIMIT", price=price, validity="DAY", tag="SHPTEST"))
            check("place order", ok, f"order {d.get('order_id')} in {(time.time() - t0) * 1000:.0f} ms" if ok else str(d))
            if ok:
                oid = d["order_id"]
                try:
                    row, s = wait_for(k, oid, lambda r: str(r.get("status")).upper() == "OPEN")
                    check("order reaches OPEN", s is not None, f"after {s}s" if s is not None else f"status stays {row and row.get('status')}")
                    t1 = time.time()
                    ok2, d2 = k.call("PUT", f"/orders/regular/{oid}", tries=1, data=dict(order_type="LIMIT", price=new_price))
                    check("modify order (price)", ok2, f"to {new_price} in {(time.time() - t1) * 1000:.0f} ms" if ok2 else str(d2))
                    if ok2:
                        row, s = wait_for(k, oid, lambda r: abs(float(r.get("price") or 0) - new_price) < 0.001 and str(r.get("status")).upper() == "OPEN")
                        check("modify confirmed in the order book", s is not None, f"after {s}s (the manager waits for this before it cancels)" if s is not None else f"row: {row and (row.get('status'), row.get('price'))}")
                finally:
                    ok3, d3 = k.call("DELETE", f"/orders/regular/{oid}", tries=2)
                    check("cancel order", ok3, "sent" if ok3 else str(d3))
                    row, s = wait_for(k, oid, lambda r: str(r.get("status")).upper() in ("CANCELLED", "REJECTED", "COMPLETE"))
                    st = str(row.get("status")).upper() if row else "?"
                    check("order shows CANCELLED", st == "CANCELLED", f"status {st}" + (f" after {s}s" if s is not None else ""))
                    if st == "COMPLETE": log("!!! the test order FILLED - check Kite and sell the position by hand")
            # nothing left behind
            ok, rows = k.call("GET", "/orders", tries=2)
            left = [o for o in rows or [] if o.get("tag") == "SHPTEST" and str(o.get("status")).upper() in M.Manager.OPEN_STATES]
            check("no SHPTEST order left open", ok and not left, f"{len(left)} left" if left else "")
            pos = k.call("GET", "/portfolio/positions", tries=2)[1] or {}
            held = [p for p in pos.get("net", []) if p.get("tradingsymbol") == sym and int(p.get("quantity") or 0)]
            check("no position in the test option", not held, f"{len(held)} open" if held else "")

    bad = [n for n, ok in RESULTS if not ok]
    log(f"SUMMARY: {len(RESULTS) - len(bad)} passed, {len(bad)} failed" + (": " + "; ".join(bad) if bad else " - everything works"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
