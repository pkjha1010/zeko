"""
kite_modify_test.py  -  TEMPORARY TEST (delete it after use).  Does Zerodha accept a price MODIFY on a pending order, through the API, from your whitelisted IP?

How to use
  1. In the Kite app place ONE pending order by hand: a LIMIT BUY priced clearly BELOW the market (so it stays open), on an option that is NOT in today's plan.
     (Before the market opens use an AMO order - same test.)  Note its order id (Orders tab -> tap the order).
  2. Run the workflow "KITE modify-price TEST" (kite_modify_test.yml) with that order id.   Locally:  ORDER_ID=123456789 python kite_modify_test.py
  3. The log shows the order BEFORE, the modify answer from Zerodha, and the order AFTER.  The last line says PASS or FAIL.

What it does to your order: ONE call  PUT /orders/<variety>/<order id>  with  order_type=LIMIT  and price = old price x (1 + PCT %)  (PCT default -1: a BUY moves 1 % DOWN, so it stays pending).
It touches ONLY the order id you give (or the only open order if there is exactly one).  Nothing else is placed, cancelled or changed - unless CANCEL_AFTER=1 (then it cancels that same order at the end).
A positive PCT on a BUY moves the price towards the market and the order may FILL.  DRY_RUN=1 prints what would be sent and sends nothing.
This tests the plain modify call.  It cannot show how an order made by an ATO alert (a market order turned into a limit order) behaves - only a live day shows that.
"""
import os, sys, time
import shape_live_manager as M

ORDER_ID = (os.environ.get("ORDER_ID") or "").strip()
PCT = float(os.environ.get("PCT") or -1)
DRY = (os.environ.get("DRY_RUN") or "0") == "1"
CANCEL_AFTER = (os.environ.get("CANCEL_AFTER") or "0") == "1"
SHOW = ("order_id", "tradingsymbol", "transaction_type", "variety", "order_type", "product", "status", "price", "quantity", "filled_quantity", "modified", "tag")


def show(title, o):
    print(f"{title}: " + ", ".join(f"{k}={o.get(k)}" for k in SHOW), flush=True)


def main():
    kite, key, tok = M.login(); k = M.K(key, tok)
    ok, rows = k.call("GET", "/orders", tries=3)
    if not ok: sys.exit(f"cannot read the order book: {rows}")
    opened = [o for o in rows or [] if str(o.get("status", "")).upper() in M.Manager.OPEN_STATES]
    print(f"{len(rows or [])} orders today, {len(opened)} of them open / pending:", flush=True)
    for o in opened: show("  open", o)
    if ORDER_ID:
        pick = [o for o in opened if str(o.get("order_id")) == ORDER_ID]
        if not pick: sys.exit(f"FAIL: order {ORDER_ID} is not among the open / pending orders (already filled, cancelled or a wrong id)")
    elif len(opened) == 1: pick = opened
    else: sys.exit("Give ORDER_ID (the number of the pending order you placed): there are " + ("no" if not opened else str(len(opened))) + " open orders.")
    o = pick[0]; oid = str(o["order_id"]); variety = o.get("variety") or "regular"
    base = float(o.get("price") or 0)
    if base <= 0: sys.exit(f"FAIL: order {oid} has no limit price (order type {o.get('order_type')}) - place a LIMIT order for this test")
    new = M.tick(base * (1 + PCT / 100))
    if new == base: new = round(base + (0.05 if PCT > 0 else -0.05), 2)
    if new <= 0: sys.exit("FAIL: the new price would be zero or negative")
    show("BEFORE", o)
    print(f"-> modify {o.get('tradingsymbol')} {o.get('transaction_type')} order {oid} ({variety}): price {base} -> {new} ({PCT:+g} %), order_type LIMIT", flush=True)
    if o.get("transaction_type") == "BUY" and new > base: print("   note: the price goes UP - if it reaches the market the order will FILL", flush=True)
    if DRY: print("DRY_RUN: nothing sent."); return
    t0 = time.time()
    ok, d = k.call("PUT", f"/orders/{variety}/{oid}", tries=1, data=dict(order_type="LIMIT", price=new))
    print(f"Zerodha answer after {(time.time() - t0) * 1000:.0f} ms: " + ("OK " + str(d) if ok else f"REFUSED: {d}"), flush=True)
    after = None
    for _ in range(10):                                           # up to 5 s for the order book to show the change
        time.sleep(0.5)
        ok2, rows2 = k.call("GET", "/orders", tries=2)
        after = next((x for x in rows2 or [] if str(x.get("order_id")) == oid), None) if ok2 else None
        if after and (abs(float(after.get("price") or 0) - new) < 1e-9 or str(after.get("status")).upper() not in M.Manager.OPEN_STATES): break
    if after: show("AFTER ", after)
    changed = bool(after) and abs(float(after.get("price") or 0) - new) < 1e-9
    if CANCEL_AFTER and after and str(after.get("status")).upper() in M.Manager.OPEN_STATES:
        ok3, d3 = k.call("DELETE", f"/orders/{variety}/{oid}", tries=1); print("cancel the test order: " + ("OK" if ok3 else f"not done ({d3})"), flush=True)
    if ok and changed: print(f"RESULT: PASS - Zerodha accepted the modify and the order now shows price {new}.", flush=True)
    elif ok and after and str(after.get("status")).upper() == "COMPLETE": print("RESULT: PASS - the modified order was filled (its new price reached the market).", flush=True)
    else: sys.exit("RESULT: FAIL - the modify was refused or the order book does not show the new price (see the lines above).")


if __name__ == "__main__":
    main()
