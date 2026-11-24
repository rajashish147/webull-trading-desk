# Webull Desk: daily-use guide

This guide assumes the project is already installed and configured. It covers
**running the dashboard and performing actions**, not installation or credentials.
The ordinary button sequence below describes the official **Webull API sandbox**,
which is separate from the Webull app's Paper Trading portfolio.

**For a live account, use [LIVE_TRADING.md](LIVE_TRADING.md) for the additional
profile, typed-arm, and per-order confirmation steps. Never treat a live profile
as paper trading.** Profile switching changes account context and halts trading.

## Quick daily workflow

**Start → Connect → Review account/data → Draft → Check/preview → Enable trading →
Submit → Inspect/reconcile → Modify/cancel if needed → Halt before finishing.**

Only explicit submission sends a new broker order. Connecting, fetching data,
checking risk, creating a draft, and previewing do not place one.

## 1. Run and connect

If the server is already running, do not start another copy. Otherwise, open
PowerShell in the project folder and run:

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

Keep that terminal open and visit **http://127.0.0.1:8000**. If you started with a
different port, use that port instead.

1. Click **Connect Webull**.
2. Approve verification in the Webull App if requested.
3. Confirm the environment badge says **SANDBOX** and the account is **Individual Cash**.
4. On **Overview**, inspect cash, buying power, portfolio value, P&L, and positions.

Every server startup begins with **Trading halted**. This is expected. You can
review data and prepare drafts before enabling order submission.

## 2. Use the dashboard pages

| Page | What you do there |
|---|---|
| **Overview** | Review account balances/positions, inspect quotes, and use the order ticket. |
| **Orders** | Inspect local orders and perform preview, submission, modification, cancellation, and reconciliation. |
| **Market data** | Edit your local watchlist, fetch snapshots, and view historical prices/OHLCV bars. |
| **Activity** | Read broker history and the application's audit trail. |
| **Settings** | Review the selected environment, non-secret configuration, risk limits, and local order counts. |

The top **Refresh** button reloads account data, quotes, and local information.
The dashboard does not continuously stream prices or automatically refresh its
tables. Prices shown on screen are snapshots, not guaranteed executable prices.

## 3. Fetch quotes and historical prices

### Watchlist and quotes

1. Open **Market data**.
2. Enter comma-separated tickers in **Symbols, comma-separated**.
3. Click **Save watchlist** to save the list locally.
4. Click **Fetch quotes** to request current snapshots.

Saving the watchlist is not the same as fetching prices. The local watchlist does
not change the Webull app's watchlist. You may request up to 100 symbols.

On **Overview**, selecting a symbol in **Market watch** fills the order ticket.
It does **not** create or submit an order.

### Historical data

1. In **Market data → Historical prices**, enter a symbol.
2. Choose an interval: 1/5/15 minutes, 1 hour, daily, or weekly.
3. Enter the number of bars, between 1 and 1,200.
4. Click **Fetch bars**.

The graph shows closing prices. The table shows open, high, low, close, and volume;
for large requests it displays the latest 100 rows. Fetching historical data does
not trade.

## 4. Prepare an order without submitting it

On **Overview → Order ticket**:

1. Enter the **Symbol** you intend to test.
2. Choose **Buy** or **Sell**.
3. Enter a positive whole number of **Shares**.
4. Enter your reviewed **Limit price (USD)**.
   - **Use quote** fills the current ask for a buy or bid for a sell.
   - Using a quote is only a convenience; review the price yourself.
5. Choose **Duration**: Day or Good till cancelled.
6. Click **Check risk** and read the result.
7. Click **Create offline draft**.

The draft is stored in the local ledger and its details dialog opens. Nothing has
been submitted to Webull. You can also create a draft before connecting, but risk
checks and broker previews require a connection.

The system supports whole-share **LIMIT BUY/SELL**, DAY/GTC, regular-hours CORE
orders. It does not support market/stop orders, options, fractional shares, or
short selling.

### Understanding limits

- **Buy limit:** sets the maximum purchase price you are willing to pay.
- **Sell limit:** sets the minimum sale price you are willing to accept.
- A limit order may fill immediately, partially, later, or not at all.
- **DAY:** valid for the broker's applicable trading day.
- **GTC:** remains active under the broker's GTC rules until filled, cancelled,
  expired, or otherwise terminated. It can remain active after this app shuts down.

## 5. Preview the draft

In the draft dialog, click **Preview & risk**. To reopen it later:
**Orders → Details → Preview & risk**.

This runs risk checks and, if allowed, requests Webull's estimated cost/fees.
It does **not** submit the order.

Risk previews can pass while trading is halted. That does not enable submission.
The backend checks risk again with fresh broker data when you actually submit.

If the draft has the wrong symbol, side, quantity, price, or duration, cancel the
unwanted draft and create a corrected one. **Modify** is for already submitted
open orders, not drafts.

## 6. Enable trading and explicitly submit

**This section sends an order to the sandbox. It may fill.**

1. Review the account, order details, risk result, and preview.
2. Close the order dialog using **×**, if it is open.
3. At the top of the dashboard, click **Enable sandbox trading**.
4. Read the confirmation, check the review checkbox, and confirm enablement.
5. Open **Orders**, locate the intended draft, and click **Details**.
6. Verify its symbol, side, shares, limit, and duration again.
7. Click **Submit sandbox order**.
8. Read the confirmation, check the review checkbox, and confirm submission.

Enabling trading does not place an order by itself. Submission does not guarantee
a fill. Read the resulting order state and filled quantity.

If you receive a timeout or an **UNKNOWN** outcome, do not create a replacement
order or blindly resubmit. Follow the reconciliation instructions below.

## 7. Monitor and reconcile orders

Open **Orders** and use:

- **Details:** inspect status, total shares, filled quantity, limit, and warnings.
- **Full order record:** expand the detailed local record, including IDs and any
  returned fill information.
- **Reconcile:** in order details, request a broker lookup for that order.
- **Reconcile active:** request broker reconciliation for up to five sandbox (two live) active local
  orders. Other active records can be handled on later requests/cycles.
- **Refresh ledger:** reload the local table; this is not itself a broker lookup.
- Top **Refresh:** refresh the account/positions and other dashboard data.

The backend periodically reconciles active local orders while connected, but the
browser table is not a live push feed. After a fill, refresh **Overview** and verify
positions and buying power before deciding on another action.

| State | Meaning / next action |
|---|---|
| **DRAFT** | Local only; preview, submit deliberately, or cancel the draft. |
| **SUBMITTING** | Submission is in progress or was interrupted; do not duplicate it. |
| **ACKNOWLEDGED** | Broker acknowledgement received; broker-detail confirmation is still needed. |
| **OPEN** | Broker reports an active order; inspect filled quantity. |
| **PARTIALLY FILLED** | Some shares filled; the remaining portion may still be active. |
| **MODIFYING** | Replacement is pending; reconcile rather than sending repeated changes. |
| **CANCELLING** | Cancellation is pending; do not assume it is cancelled yet. |
| **FILLED** | Full quantity filled; refresh account positions. It cannot be cancelled. |
| **CANCELLED** | Cancelled, including cancelled local drafts. Any earlier fills remain. |
| **REJECTED / EXPIRED** | Not an active working order; inspect details/history. |
| **UNKNOWN** | Outcome is uncertain; reconcile and review full broker activity before doing anything else. |

An order missing from open orders may have filled. Missing from a fresh PC's local
ledger also does not mean it never existed at Webull.

## 8. Modify an open order

1. Confirm the account is connected and sandbox trading is enabled.
2. Open **Orders → Details** for an **OPEN** or **PARTIALLY FILLED** order.
3. Click **Modify**.
4. Enter the new **Total shares** and **Limit price (USD)**.
5. Review them, then click **Send replacement**.
6. Inspect the returned state and reconcile if the change remains pending.

**Send replacement immediately requests a broker change.** The backend reruns
risk checks. Only quantity and limit price are editable through this action.

Quantity means the **new total**, including existing fills—not additional shares
or remaining shares. For example, if 2 of 5 shares have filled and you want 2 more
remaining, the new total is 4. The new total must exceed the filled quantity.

If the order fills or becomes terminal before modification, it may no longer be
modifiable. A pending/unknown mutation must be reconciled before another change.

## 9. Cancel an order or draft

1. Open **Orders → Details** for the intended record.
2. Click **Cancel order** or **Cancel draft**.
3. Read the confirmation, check the review checkbox, and confirm cancellation.
4. Inspect the final state; reconcile if it remains **CANCELLING** or uncertain.

- Cancelling a **draft** removes that local intent from the active workflow; it
  never became a broker order.
- Cancelling a **submitted order** requests broker cancellation. It may already
  have filled, or fill before cancellation completes.
- Cancellation is available while trading is halted, provided the account is
  connected and no conflicting mutation is pending.
- Cancelling a partially filled order does not undo the shares already traded.
- A **FILLED** order cannot be cancelled. Cancelling does not sell an existing
  position or reverse a completed trade.

## 10. Sell or reduce a position

1. Use top **Refresh** and check **Overview → Your positions**.
2. In **Order ticket**, choose that symbol and **Sell**.
3. Enter a whole-share quantity you own and a reviewed sell limit.
4. Check risk, create the draft, preview, and submit through the normal workflow.
5. Reconcile and refresh positions afterward.

Sell orders are long-only. The system checks held shares and shares reserved by
other pending sell orders. A sell limit may not fill, so submitting it is not proof
that the position has closed. No automatic strategy or liquidation runs for you.

## 11. Review history, logs, and settings

### Activity

- **Load history:** fetch a read-only page of Webull broker order history.
- **Next history page:** load another page if available.
- **Refresh events:** load the application's audit events.
- **Load next events:** move to the next audit-event page.

Broker history can include orders created outside this desk. The **Orders** page
only manages this local backend's records. On a fresh PC with a new database,
old-PC orders are not automatically imported; use the original ledger or Webull's
own tools to manage orders created elsewhere. Never recreate an order solely
because it is missing from the new local table.

### Settings

Review environment, account, risk limits, and **Local order report**. The report
counts ledger records, including cancelled drafts; it is not a realized-profit
report or proof of the number of executed trades. Financial P&L comes from Webull.

If you deliberately edit settings in `.env`, restart the server to apply them.
There is no need to rerun installation/provisioning for an ordinary daily session.

## 12. Finish a session safely

1. Click **Halt trading** to block new submissions and replacements.
2. Review **Orders** for active or uncertain records.
3. Explicitly cancel unwanted active orders and confirm their broker states.
4. Refresh **Overview** and review any remaining holdings.
5. Press **Ctrl+C** in the server terminal when ready to stop the application.

**Halting, closing the browser, and stopping the server do not automatically
cancel broker orders or close positions.** DAY/GTC orders and filled positions
can remain at Webull. If you intend to keep them, do so deliberately; if you intend
to close a holding, use the reviewed sell-order workflow.

For an optional private backup of the local ledger:

```powershell
.\.venv\Scripts\python.exe manage.py backup
```

Keep databases/backups private. Never delete a ledger to bypass an unknown result.

## Common issues

| What you see | What to do |
|---|---|
| **Check risk / Use quote / Fetch quotes disabled** | Connect Webull first. |
| **Submit sandbox order disabled** | Confirm connection, SANDBOX, enabled trading, and a DRAFT state. Close the order dialog before using the top enable button. |
| **Risk rejection** | Read every violation. Review settled cash, held shares, open orders, size/exposure/loss limits, and your limit price. Do not disable safeguards just to bypass it. |
| **Price deviation too large** | Fetch a fresh quote and review your intended limit. The default deviation limit is 10%; an extremely distant test price can be blocked. |
| **Stale quote / not regular hours** | Refresh prices and verify market session/clock. CORE generally follows US regular hours, normally 9:30 AM–4:00 PM New York time on trading days, subject to holidays/early closes. |
| **UNKNOWN / pending action** | Use Reconcile, inspect broker activity, and do not blindly resubmit or create a new order. It may already have succeeded. |
| **FILLED but positions look old** | Reconcile the order and use top Refresh; do not assume it failed because the browser table was stale. |
| **Cancellation pending** | Reconcile until broker state is confirmed. A fill may have occurred before cancellation. |
| **Session expired** | Reload the dashboard. Do not re-send an uncertain order as a workaround. |
| **Rate limit / 429** | Stop repeated clicking and retry reads later. Do not blindly retry broker mutations. |
| **Webull verification wait** | Approve the request in the Webull App; no password or secret is entered into this dashboard. |

See [API.md](API.md) for deeper state, risk, and recovery details. This guide is
about the sandbox workflow. Optional live execution has additional safeguards
documented in the live guide; autonomous trading is not included.
