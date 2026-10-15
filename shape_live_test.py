"""
SHAPELIVE TEST  -  checks the static-IP proxy and the order paths of the manager with ONE lot, during market hours.
It never touches alerts and never uses the plan's quantities: it trades exactly 1 lot of one option.

  python shape_live_test.py cancel     BUY limit far BELOW the market (cannot fill) -> the manager's own "cancel an
                                       unfilled BUY" code must cancel it.  Costs nothing.  Proves: place + cancel through the proxy.
  python shape_live_test.py roundtrip  BUY 1 lot at market, hold HOLD_S seconds (default 15), SELL exactly the bought quantity.
                                       Costs the spread + charges of one small round trip.  Proves: place + sell through the proxy.

Option used: TEST_OPTION (e.g. OIL26OCT445PE) or, if empty, the first option in live_plan/plan.json.
If the option is not in the plan also give TEST_LOT (lot size) and TEST_PRICE (approximate option price).
DRY_RUN=1 prints what would be sent and sends nothing.
"""
import os, sys, json, time
from datetime import time as dtime
import shape_live_manager as M

log = M.log


def pick():
    opt = (os.environ.get("TEST_OPTION") or "").strip()
    items = json.load(open(M.PLAN))["items"] if os.path.exists(M.PLAN) else []
    it = next((i for i in items if i["option"] == opt), None) if opt else (items[0] if items else None)
    lot = int(os.environ.get("TEST_LOT") or 0) or (int(it["lot_size"]) if it else 0)
    px = float(os.environ.get("TEST_PRICE") or 0) or (float(it["cost"]) / float(it["qty"]) if it and it.get("qty") else 0)
    sym = opt or (it["option"] if it else "")
    if not (sym and lot and px): sys.exit("Give TEST_OPTION, TEST_LOT and TEST_PRICE (no usable plan entry found)")
    return sym, lot, px


def get_order(k, oid):
    ok, rows = k.call("GET", "/orders", tries=2)
    return next((o for o in (rows or []) if str(o.get("order_id")) == str(oid)), None) if ok else None


def wait_status(k, oid, want, secs, on_each=None):
    t0, o = time.time(), None
    while time.time() - t0 < secs:
        o = get_order(k, oid)
        if o and on_each: on_each(o)
        if o and o.get("status") in want: return o
        time.sleep(0.3)
    return o


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("cancel", "roundtrip"): sys.exit("mode must be cancel or roundtrip")
    t = M.now_ist().time()
    if not M.DRY_RUN and not (dtime(9, 20) <= t <= dtime(15, 20)): sys.exit(f"Market is not open now ({t:%H:%M}) - run between 09:20 and 15:20 IST")
    sym, lot, px = pick()
    log(f"test option {sym}  1 lot = {lot}  approx price {px:.2f}  mode {mode}  proxy {'ON' if M.PROXIES else 'OFF'}")
    kite, key, tok = M.login(); k = M.K(key, tok)
    item = {"option": sym, "qty": lot, "close_price": px}
    m = M.Manager(k, {"items": [item]})

    if mode == "cancel":
        price = max(0.05, M.tick(px * 0.5))
        oid = m.place(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY", quantity=lot,
                      product=M.PRODUCT, order_type="LIMIT", price=price, validity="DAY", tag="SHPTEST")
        if not oid: sys.exit("FAIL: the order was not accepted (see the message above; 'No IPs configured' = proxy/whitelist problem)")
        if oid == "DRY": return
        o = wait_status(k, oid, ("CANCELLED", "REJECTED", "COMPLETE"), 8, on_each=m.watch_open)   # manager's own cancel logic
        st = (o or {}).get("status")
        if st == "CANCELLED": log("PASS: order placed AND cancelled through the proxy")
        elif st == "REJECTED": log(f"order rejected by the exchange: {(o or {}).get('status_message')} - IP was fine, try TEST_PRICE higher")
        elif st == "COMPLETE": log("the limit order FILLED (price moved) - sell it in Kite if you do not want it")
        else:
            log(f"FAIL: status {st} - cancelling directly"); ok, d = k.call("DELETE", f"/orders/regular/{oid}", tries=2); log(f"cancel -> {ok} {d}")
        return

    # roundtrip
    oid = m.place(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY", quantity=lot,
                  product=M.PRODUCT, order_type="MARKET", validity="DAY", market_protection=-1, tag="SHPTEST")
    if not oid: sys.exit("FAIL: BUY not accepted")
    if oid == "DRY": m.sell_market(sym, lot, "SHPTEST"); return
    o = wait_status(k, oid, ("COMPLETE", "REJECTED", "CANCELLED"), 10)
    q = int((o or {}).get("filled_quantity") or 0)
    if not o or o.get("status") != "COMPLETE":
        log(f"BUY status {(o or {}).get('status')} filled {q} - cancelling the rest")
        k.call("DELETE", f"/orders/regular/{oid}", tries=2); time.sleep(1); o = get_order(k, oid) or o; q = int(o.get("filled_quantity") or 0)
    if q <= 0: sys.exit("FAIL: nothing was bought")
    buy = float(o.get("average_price") or 0)
    log(f"BOUGHT {q} @ {buy}  - holding {os.environ.get('HOLD_S') or 15}s")
    time.sleep(float(os.environ.get("HOLD_S") or 15))
    sid = m.sell_market(sym, q, "SHPTEST")
    if not sid: sys.exit(f"FAIL: SELL not accepted - the {q} bought is still OPEN, sell it in Kite now!")
    s = wait_status(k, sid, ("COMPLETE", "REJECTED", "CANCELLED"), 10)
    if s and s.get("status") == "COMPLETE":
        sell = float(s.get("average_price") or 0); log(f"SOLD {q} @ {sell}  gross {(sell - buy) * q:+.2f}   PASS: place + sell worked through the proxy")
    else: log(f"SELL status {(s or {}).get('status')} - check Kite and close the position by hand")


if __name__ == "__main__":
    main()
