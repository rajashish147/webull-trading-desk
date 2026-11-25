# Two-stock price-sum strategy

This implements the agreed **bounded automation** policy for two distinct US
stocks/ETFs, with equal whole-share quantities. It is not atomic pair execution,
a profit/stop-loss guarantee, or proof of live readiness. Development tests use
fake brokers; real production execution still needs operator validation.

## Rule

- Observe `ask(A)+ask(B)` above Z, then buy both when a fresh snapshot crosses to
  Z or below. Arming while already below Z does not buy immediately.
- After both full quantities fill, attempt to sell both when `bid(A)+bid(B) >= Z+a`.
- Attempt a downside exit when `bid(A)+bid(B) <= Z-b`, with 0 < b < Z.
- Use captured ask/bid prices as DAY/CORE limits. Either order can remain unfilled.
- Repeat only after a flat completed cycle, cooldown, and a new crossing.
- `max_cycles` counts **entry attempts**, including failed attempts.

Thresholds are raw per-share price sums, not net realized profit. Fees, spreads,
partial fills, timing and broker tax-lot selection can change financial results.

## Dashboard

Open **Strategies → Create strategy** and fill:

| Field | Meaning |
|---|---|
| A / B | Different symbols |
| Shares each | Equal whole-share quantity |
| Z / a / b | Entry, profit increment, downside delta |
| Pair budget | Bound on captured entry limits plus fee buffer |
| Max entry attempts | 1–5; finite repetition |
| Cooldown | At least 30 seconds |
| Leg timeout | 10–120 seconds |
| Authorization | 30–600 seconds |

### Automatic quote and cost preview

After connecting, entered valid symbols are fetched automatically. Typing is
debounced, and requests are spaced by at least five seconds. With **Auto-update
entered stocks** enabled, quotes refresh every 15 seconds only while the Strategies
page and browser tab are visible. Quota errors back off for 60 seconds.

The preview shows each stock's last/bid/ask, quote age and market state, plus:

- Current **per-share** ask(A)+ask(B) and bid(A)+bid(B).
- Entry value for both legs × **Shares each**, and a fee-buffer estimate.
- Distances to Z, Z+a and Z−b; theoretical gross differences, not net P&L.
- Current spread × shares, and bid-side position-value estimate.
- Warnings for stale/incomplete/skewed quotes, pair budget, per-leg quantity/value
  caps, invalid downside settings, and insufficient daily submission capacity.

Changing shares recalculates immediately and queues a debounced quote refresh.
Use **Refresh prices** for a manual request, **Swap A / B** to swap form symbols,
or **Use current ask sum as Z** to deliberately copy a fresh sum into the threshold.
Auto-refresh never overwrites Z/a/b, modifies a saved strategy, arms it, or trades.
Profile changes clear old quotes and form thresholds, and late old-profile/symbol
responses are discarded. Preview arithmetic is advisory; backend Decimal-based
risk and strategy checks still decide whether a trade is allowed.

**Save disabled strategy** does not arm or trade. Connect and enable sandbox,
then click **Arm sandbox strategy**, review the policy, type **ARM SANDBOX STRATEGY**
and confirm. It can now trade automatically within that window without per-order
clicks. Only one strategy may be armed per profile.

The backend normally evaluates every five seconds; the browser need not stay open.
Live automation cannot arm if the backend worker is disabled (reconcile interval 0).
**Refresh strategies** updates the monitor. **Evaluate once** can dispatch orders
if already armed; it never arms an inactive strategy.

Snapshots require both unique symbols, positive uncrossed bid/ask, regular hours,
age no more than 15 seconds (or stricter global limit), and timestamp skew no more
than two seconds. Invalid/stale data skips signals and resets the crossing baseline.

## Non-atomic legs and recovery

Both local intents are persisted before dispatch, with stable per-cycle/leg keys.
Orders are sent sequentially through ordinary risk and SDK permit checks. They
cannot be guaranteed to fill together.

If a leg fails, is uncertain, or times out: stop sending further legs, reconcile,
cancel remaining known working legs **while authorization is valid**, and halt for
manual review. Unsent drafts are cancelled locally. Never blindly retry an ambiguous
mutation. **Filled exposure is not automatically reversed**—the selected policy.
The same failure on exit can leave one stock held. A limit downside exit is not a
guaranteed stop-loss or liquidation.

States: DRAFT → WAITING_ENTRY → ENTERING → HOLDING → EXITING → WAITING_ENTRY/COMPLETED.
Failures go through CLEANUP, then PAUSED or RECOVERY_REQUIRED. Restart invalidates
grants and marks previously running strategies RECOVERY_REQUIRED; it never auto-arms.

For recovery:

1. **Pause automation**. This stops future requests, not existing orders/holdings.
2. Reconcile and explicitly cancel unwanted linked orders through Orders.
3. Inspect actual holdings and clear pending/unknown outcomes.
4. If both full planned quantities are owned and no order is working, a fresh arm
   can resume HOLDING with the original exit thresholds.
5. Incomplete/unbalanced fills cannot auto-resume. When all linked orders are
   terminal and holdings match, **Release to manual recovery** with
   **RELEASE STRATEGY HOLDINGS** removes reservations without placing a trade.
6. Manage released shares with reviewed manual orders or Webull's own tools.

Released strategies cannot restart. Working/uncertain orders and inconsistent
allocations block release. Never delete a ledger or recreate orders to bypass uncertainty.

Owned quantities derive only from linked buy fills minus linked sell fills; they
reserve shares from unrelated manual sells. This is **quantity allocation, not
tax-lot selection**. Do not trade these symbols outside the strategy while relying
on allocation; the app cannot atomically control external account activity.

## Bounded live authorization

Keep both false until sandbox testing and production read-only validation:

```dotenv
WEBULL_ENABLE_LIVE_TRADING=false
TRADING_ENABLE_LIVE_STRATEGIES=false
TRADING_STRATEGY_MAX_PAIR_NOTIONAL=1000
TRADING_STRATEGY_POLL_SECONDS=5
```

To deliberately use live automation, review limits, opt in locally and restart,
select/connect Live, complete the global live arm, then separately type
**ARM LIVE STRATEGY** for the exact policy. The strategy cannot renew its window
and never outlives global live arming. Every leg is checked against the registered
account/environment, immutable policy, current cycle/intent, ownership, budget,
global risk, expiry and an exact SDK permit.

This is the explicit exception to manual per-order confirmation: the reviewed
strategy grant authorizes its own buys, sells and cancellations during the window.
Ordinary manual/API trades retain their one-use live approvals.

Remaining cycles must fit the global daily order limit at arming. One full cycle
normally needs four submissions. The live default of five does **not** silently
allow two full cycles; choose one or deliberately review/configure a suitable cap.
Pair budgets and ordinary per-leg/exposure/loss limits all apply.

Halt, pause, expiry and shutdown invalidate authorization. They do **not** close
positions or cancel existing broker orders automatically. After expiry, new cleanup
or liquidation requests need fresh manual authorization. Profile switching halts
all profiles and is blocked by active/uncertain orders or reserved strategy holdings.

## API endpoints

- `POST /v1/strategies`: save a disabled specification.
- `GET /v1/strategies` or `GET /v1/strategies/{id}`: inspect states, ownership and linked orders.
- `POST /v1/strategies/{id}/arm`: typed ARM SANDBOX STRATEGY or ARM LIVE STRATEGY.
- `POST /v1/strategies/{id}/pause`: stop future automation.
- `POST /v1/strategies/{id}/tick`: evaluate once; can dispatch if authorized.
- `POST /v1/strategies/{id}/release`: typed manual-allocation release.

Production writes require `X-Trading-Environment: production`. The API console
adds live review for arm/tick and cannot bypass the strategy grant.

Example only—not current prices or a recommendation:

```json
{"name":"Pair test","symbol_a":"AAPL","symbol_b":"MSFT","quantity":1,
 "entry_sum":"200","profit_increment":"4","downside_delta":"10",
 "max_pair_notional":"500","max_cycles":2,"cooldown_seconds":60,
 "order_timeout_seconds":30,"duration_seconds":300}
```

Review and choose suitable values. An unsuitable example may never trigger or fail risk.

## Limits and validation

Schema 1/2 is backed up before migration to schema 3. Policies, cycles, intents and
fills persist; grants do not. Credentials, ledgers and backups remain private.
Tests cover crossing/exit/repeat/cooldown, stale/skewed data, timeouts, failed legs,
ownership, restart and narrow live permissions using fake brokers.

Market gaps, stale data, partial fills, fees, rate limits, halted symbols, outside
activity, provider changes, power/network failure and expiry can prevent exit.
No real production strategy execution has been tested during development.
Supervise live use; "all edge cases" and guaranteed hedging/profit are not claims
this software can make. See [LIVE_TRADING.md](LIVE_TRADING.md) for live operating safeguards.
