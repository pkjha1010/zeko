# Replace these files in the `zeko` repo (same names as before)

| copy this file | to the repo |
|---|---|
| `shape_live_plan.py`          | repo root |
| `shape_live_manager.py`       | repo root |
| `shape_live_offline_test.py`  | repo root |
| `.github/workflows/shape_live_plan.yml`          | `.github/workflows/` |
| `.github/workflows/shape_live_manager.yml`       | `.github/workflows/` (two chained jobs - REPLACE the whole file) |
| `.github/workflows/shape_live_offline_test.yml`  | `.github/workflows/` |
| `CRON_JOB_ORG.md`  | repo root (the cron-job.org setup: the 12:55 manager call is no longer needed) |

No new secrets (KITE_*, ZERODHA_*, ANGEL_*, ORDER_PROXY as before).  Run `shape_live_offline_test.yml` first - it must print `ALL OFFLINE TESTS PASSED`.

## The two live strategies (backtest, target-gap trades removed)
| code | strategy | direction | stop-loss | target | entries | square off | trades at the same time |
|---|---|---|---|---|---|---|---|
| WRU | WICK_RED_UPPER (red signal candle, upper wick longer) | SHORT (buy put) | -20 % | none | 09:15 - 09:29 | 09:30:00 | no limit (it has a stop-loss) |
| BPB | BREAKOUT_BPB (breakout -> pullback -> re-cross of the 10-day low) | SHORT (buy put) | none | +60 % | 09:15 - 15:28 | 15:30:00 | **at most 3** (it has no stop-loss) |
WRL is still in the code (`STRATEGIES=WRU,WRL,BPB`) but is not planned any more.  Each trade may use 10 % of the funds available in Kite when the MORNING plan runs.
The old 30 % portfolio limit is OFF (`fund_limit_pct` default 0; give 30 to switch it on again).

## Daily routine
1. ONCE in the evening (18:00, after the market has closed): run `shape_live_plan`.  It plans the NEXT trading day, sizes each trade on the funds in Kite at that moment (10 %), replaces the old SHP| alerts,
   creates the new alerts and saves `plan.json` (table on the run's Summary tab).  The run must be green.  If your funds change a lot in the night, give `total_funds` instead.
2. 08:55 next morning: the manager starts once (cron-job.org) and hands over from the morning job to the afternoon job at 12:58 by itself.
3. Missed the evening run?  Run the plan before 09:15 - it plans that same day.

## Safety rules in the manager
1. an unfilled BUY order is cancelled after 0.5 s;
2. an unfilled stop-loss SELL is cancelled after 0.5 s and the position is squared off at once (MARKET, then ever more aggressive LIMIT orders until flat); an unfilled target SELL the same after 3 s;
3. BPB: while 3 BPB positions are open its waiting alerts are deleted and waiting BUYs cancelled; when one closes (target / 15:30) the alerts of setups that have not traded AND whose entry
   level has not been crossed yet are re-created; a 4th fill that slips through (two alerts firing together) is sold at once (tag SHPCAP);
4. each strategy has its own entry end and square-off; a BUY that is filled after the entry end is sold at once;
5. at the exit time the strategy's GTTs are deleted, waiting orders cancelled and positions squared off, then a sweep repeats until two passes find nothing;
6. only plan options / SHP* orders are touched; SWG alerts, swing positions and other orders are never touched (`SWEEP_ALL=0`);
7. `DRY_RUN=1` sends nothing.  The manager can be started again at any time: it rebuilds its state from Kite.  At 15:45 anything left makes the run FAIL (mail).

## Still to verify live
* The stop-loss GTT (WRU) has never run live: on the first WRU trade look at Kite that a GTT with a -20 % stop-loss exists, and in the manager log the line "stop-loss ... still waiting" if it ever triggers.
* The 3-trade cap for BPB cannot stop two alerts that fire in the same second; the 4th fill is then sold at once (small spread cost).
