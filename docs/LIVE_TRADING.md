# Controlled live trading, with sandbox retained

**Strategy automation is a separate opt-in.** The manual workflow below still
requires per-order approvals. A strategy authorized with **ARM LIVE STRATEGY**
may execute its own bounded buy/sell/cancel policy without per-order clicks while
both its grant and global live arm are valid. Read [STRATEGIES.md](STRATEGIES.md)
before enabling `TRADING_ENABLE_LIVE_STRATEGIES`; it remains false by default.

Version 2 adds live execution for a **Webull US Individual Cash account**, while
retaining a separate sandbox profile. It is a **personal, supervised, whole-share
LIMIT BUY/SELL tool**, not a broker-certified or guaranteed production platform.
No real-money orders are placed by installation, provisioning, tests, connecting,
profile switching, risk checks, or cost previews.

Development execution tests use mock brokers. A production account connection,
market-data entitlements, and real execution must still be validated by the
operator. Do not interpret passing mock tests as proof of production fills,
settlement, reliability, or financial performance.

## Safety layers

1. Separate sandbox/live App Keys, App Secrets, Account IDs, SDK clients and token caches.
2. Separate account/environment-scoped SQLite ledgers and risk limits.
3. Sandbox is the default startup profile; every profile starts halted/disarmed.
4. Production never falls back to generic or sandbox credentials.
5. Live submission/replacement requires an explicit `.env` opt-in.
6. Live arming requires a validated Cash account, complete USD account data, and
   the exact phrase **ENABLE LIVE TRADING**. The default window is 10 minutes.
7. Every manual live submit/replace/cancel requires a **60-second, one-use approval** bound
   to the account, environment, order, operation, parameters, and order revision.
8. The final action requires a typed acknowledgement. The SDK adapter also checks
   a short-lived exact-action execution permit before sending to Webull.
9. No automatic broker mutation retries. Ambiguous outcomes remain uncertain until reconciled.
10. All-profile halt invalidates live arms/approvals. Emergency cancellation does
    not require an active live arm, but still requires explicit order confirmation.

These are application controls, not broker-level permissions or protection from
another program using your credentials. Keep the application on localhost and do
not concurrently trade this account elsewhere while relying on its risk controls.

## Configure live without overwriting sandbox

Stop the server before changing configuration/updating code. Existing broker
orders can remain active after shutdown, so reconcile and review them first.

For an existing installation, add the new blank fields while preserving your
existing credentials and flags:

```powershell
.\.venv\Scripts\python.exe manage.py upgrade-env
```

Keep the old generic `WEBULL_APP_KEY`, `WEBULL_APP_SECRET`, and `WEBULL_ACCOUNT_ID`
as **sandbox-only** values, or fill all three explicit sandbox fields:

```dotenv
WEBULL_ENV=sandbox
WEBULL_SANDBOX_APP_KEY=
WEBULL_SANDBOX_APP_SECRET=
WEBULL_SANDBOX_ACCOUNT_ID=
```

Enter your approved production credentials **locally**:

```dotenv
WEBULL_LIVE_APP_KEY=
WEBULL_LIVE_APP_SECRET=
WEBULL_LIVE_ACCOUNT_ID=
WEBULL_ENABLE_LIVE_TRADING=false
```

Do not copy sandbox values into live fields. Keys, secrets, and account IDs must
be distinct across configured profiles. Never paste them into GitHub/chat/issues.
If the live Account ID is not yet known, the following command only queries and
validates accounts and writes the selected live Cash ID locally:

```powershell
.\.venv\Scripts\python.exe manage.py init --environment production
```

It uses **only the live App Key/Secret**, preserves existing secrets, and never
submits an order or arms trading. If an explicit ID is configured, it must match
the account list; an invalid ID is not silently replaced. Ambiguous selection fails.

To provision sandbox separately:

```powershell
.\.venv\Scripts\python.exe manage.py init --environment sandbox
```

The backend API key remains local and shared by the dashboard service. It is not
your Webull App Key. The dashboard still needs no key-entry screen.

## Validate production in read-only mode first

Keep `WEBULL_ENABLE_LIVE_TRADING=false` and start the server:

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

1. Open http://127.0.0.1:8000.
2. Select **Live (real money)** in the **Profile** selector.
3. Type **SWITCH TO LIVE**, check the review box, and confirm.
4. Verify the red **LIVE · REAL MONEY** banner.
5. Click **Connect Webull** and approve App 2FA if requested.
6. Verify the actual Cash account and masked Account ID, balances and positions.
7. Fetch quotes and historical data; check OpenAPI market-data permissions/entitlements.
8. In **Orders**, load **Broker open orders**; inspect **Activity → Load history**.
9. Create an offline draft and use **Preview & risk**. This does not submit it.

`configured` only means credential/account fields exist. `connected` means SDK
initialization/account validation succeeded, not continuous availability or proof
that the account can execute every symbol. Order-specific risk/preview checks
must succeed before submission. Broker fees and fills may differ from estimates.

Live risk requires fresh regular-hours data. Delayed/stale quotes, invalid fields,
unsupported positions/orders, missing fee estimates, or non-USD settlement fail
closed. Do not turn off 2FA or weaken checks to bypass a connection/data error.

## Conservative live defaults

Live settings are independent of sandbox's larger defaults:

| Setting | Default |
|---|---:|
| `TRADING_LIVE_MAX_ORDER_QUANTITY` | 5 whole shares |
| `TRADING_LIVE_MAX_ORDER_NOTIONAL` | $500; broker estimated cost/fees must also fit |
| `TRADING_LIVE_MAX_SYMBOL_EXPOSURE` | $1,000 gross |
| `TRADING_LIVE_MAX_PORTFOLIO_EXPOSURE` | $5,000 gross |
| `TRADING_LIVE_MAX_OPEN_ORDERS` | 3 |
| `TRADING_LIVE_MAX_DAILY_ORDERS` | 5 submission attempts per UTC day |
| `TRADING_LIVE_MAX_DAILY_LOSS` | $50 broker day loss; blocks new buys |
| `TRADING_LIVE_MAX_PRICE_DEVIATION` | 0.02 (2%) |
| `TRADING_LIVE_MAX_QUOTE_AGE_SECONDS` | 30 seconds |
| `TRADING_LIVE_FEE_BUFFER` | 0.01 (1%) |
| `TRADING_LIVE_ARM_SECONDS` | 600 seconds; allowed range 30–1,800 |

The live pilot accepts cent-priced limits at/above $1 and 0.0001 increments below
$1. This intentionally rejects some finer valid broker prices. Review/adjust the
limit yourself; the app does not automatically round an order into execution.

Existing holdings above exposure limits can block new buys. Long-only sells can
reduce owned holdings, but quantity/notional/daily-order limits still apply.
Pending sells reserve shares; unsettled proceeds are not treated as settled cash.
The fee buffer is an estimate, not a fee guarantee or complete settlement model.

Review limits against your own account, goals and intended test before changing
them. This documentation is not a trade recommendation. A stock above the live
notional cap cannot be purchased even as one whole share until you deliberately
review and configure an appropriate cap.

## Enable a deliberate live session

Only after read-only validation and risk review, edit locally:

```dotenv
WEBULL_ENABLE_LIVE_TRADING=true
```

Restart the server; do not hot-edit `.env` and assume it took effect. Configuration
is loaded at startup. Restart invalidates all previous live arms and approvals.
Use **Halt all profiles** for an immediate application halt rather than editing a
file while the server is running.

1. Select Live again and connect/validate the actual account.
2. Review **Settings → Risk limits**.
3. Click **Arm live trading**.
4. Type **ENABLE LIVE TRADING**, check the box, and confirm.
5. The UI shows the arming expiry. Arming does not place any order.

When the window expires, new submissions/replacements are blocked. Expiry does
**not** cancel existing broker orders or close positions. The browser countdown
does not poll the broker; the backend independently checks expiry.

## Live submission, replacement and cancellation

### Submit

1. Prepare/review a live draft through the ordinary order ticket.
2. Open its **Details** and click **Review LIVE submission**.
3. Read the exact account, symbol, side, quantity, limit, duration, and broker preview.
4. Type **SUBMIT LIVE ORDER**, check the review box, and confirm within 60 seconds.
5. Inspect status and filled quantity. Reconcile before assuming success/failure.

The backend reruns fresh risk and cost checks, validates the exact approval, saves
a durable intent, then sends once. Acknowledgement is not a guaranteed fill.

### Replace

1. For an open/partially filled order, select **Modify**.
2. Enter a new **total** quantity and limit.
3. Click **Review LIVE replacement**.
4. Review the server-confirmed parameters; type **MODIFY LIVE ORDER**, check the
   box, and confirm within 60 seconds.

A fill or parameter/state change after approval invalidates it. Refresh and
review again rather than reusing an old confirmation. Quantity is the new total,
including fills, and must exceed the already filled quantity.

### Cancel

1. Open order Details and click **Review LIVE cancellation**.
2. Review the exact account/order; type **CANCEL LIVE ORDER**, check the box, and confirm.
3. Verify the final broker status; a cancellation request can race a fill.

Cancellation is available while disarmed, including when the file opt-in is false,
provided the production account is connected and the specific approval is valid.
Conflicting pending mutations must be reconciled first. A cancelled local draft
requires no broker execution approval because it was never submitted.

Do not blindly repeat a timed-out mutation or create a duplicate order. UNKNOWN,
ACKNOWLEDGED and pending outcomes block adding risk until resolved. There is no
unsafe "forget unknown order" bypass. Verify broker details/full activity or seek
operator/Webull support if an ambiguous result cannot be reconciled.

## Switching back to sandbox

Close order dialogs, select Sandbox, check the confirmation, and switch. Every
switch halts/disarms **all** profiles and invalidates approvals. It is blocked if
the current/target ledger has active/uncertain orders or a connected broker has
open orders. A blocked switch still halts trading; it does not cancel those orders.

Resolve/cancel working orders first. Existing filled holdings may remain at the
broker; switching never sells them. Background reconciliation monitors connected
profiles read-only, but the dashboard shows only the selected account. Prices,
ticket fields and selected-order dialogs reset, preventing old-profile actions.
Stale tabs receive an environment-change error and cannot retarget a write.

`WEBULL_ENV` selects the startup profile. Runtime selection does not rewrite it;
keeping `WEBULL_ENV=sandbox` brings you back to sandbox after a restart.

## Direct API callers

Direct callers use the backend API key, and **every production write** also needs
`X-Trading-Environment: production`. To switch, send the **currently active**
environment header to `PUT /v1/environment` with a target environment and exact
`SWITCH TO LIVE` / `SWITCH TO SANDBOX` acknowledgement.

- Arm: `POST /v1/live/arm`, body `{"confirmation":"ENABLE LIVE TRADING"}`.
- Approve: `POST /v1/orders/{id}/live-approval`, body `{"operation":"SUBMIT"}`,
  or `{"operation":"REPLACE","quantity":2,"limit_price":"YOUR_REVIEWED_PRICE"}`,
  or `{"operation":"CANCEL"}`. Use a real numeric price string, not the placeholder.
- Execute: send the returned token as `X-Live-Confirmation` and the exact returned
  phrase as `X-Live-Acknowledgement` to submit/replace/cancel. Replacement parameters
  must match those approved. Tokens are one-use and expire after 60 seconds.

Tokens must not be logged, stored in public examples, or reused. The legacy CLI
can read profile data but **cannot submit live orders**, even with its old flags.

## Recovery, backup and limitations

Schema 1/2 is backed up automatically before migration to schema 3; records keep
their IDs and gain account/environment scope. Interrupted broker mutations become
UNKNOWN, and pending live approvals are invalidated. Do not delete ledgers to
bypass uncertainty or silently move one account's ledger to another account.

Manual private backups can target either configured profile:

```powershell
.\.venv\Scripts\python.exe manage.py backup --environment sandbox
.\.venv\Scripts\python.exe manage.py backup --environment production
```

The default backup uses the `.env` startup profile, not necessarily the current UI
selection. Protect all credentials, tokens, databases and backups; they are ignored
by Git, not encrypted. Do not run two servers sharing the same profile ledgers.

The app still has no market/stop orders, fractional shares, shorting, margin
borrowing, options, push order-event integration, automated liquidation, or
production compliance certification. Account display reads may be cached for up
to two seconds; execution risk reads are always fresh. Broker rate limits can
delay reconciliation. Risk snapshots are not an atomic guarantee against outside
account activity. Supervise live use and review final broker activity/fees.
