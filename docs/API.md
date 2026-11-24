# API and operations guide

## Personal dashboard

Run `python manage.py serve` and open **http://127.0.0.1:8000**. The dashboard uses
the same API and risk controls, with no key-entry screen. The Python backend reads
its configuration directly from `.env` at startup; inherited shell variables do
not override it. Restart after editing `.env`.

The browser initializes a local HttpOnly/SameSite session and receives a separate
CSRF token. Webull secrets and the backend API key never appear in browser code,
HTML, session responses, or browser storage. This convenience mode is limited to
loopback peers and localhost hosts, rejects cross-origin requests, and does not
expose `.env` or the project directory as static files.

Use **Connect Webull**, inspect balances/quotes, and create an offline draft. Order
submission and trading enablement require separate checked confirmations. The
kill switch, sandbox-only execution, and all backend risk checks remain in place.
The UI does not automatically poll quotes or submit orders. Static assets have no
external JavaScript/font dependencies. Existing direct API key authentication
continues to work for scripts.

## Architecture

```text
HTTP API (FastAPI, X-API-Key)
    → TradingService (serialized mutations, durable intents)
        → RiskEngine (fresh USD cash, positions, open orders, quote)
        → WebullGateway (official SDK, HTTPS, no mutation retries)
        → Database (SQLite WAL, order ledger + transactional audit events)
    → Read-only reconciliation worker (5 active orders per cycle)
```

Files: `trading/app.py`, `trading/service.py`, `trading/risk.py`,
`trading/broker.py`, `trading/database.py`, `trading/config.py`, and
`trading/models.py`. The original `webull_cli.py` remains a separate tool.

The backend is single-worker/single-account. A process lock prevents two backend
workers sharing one ledger. The database path contains a hash of the configured
account and is isolated by sandbox/production; its identity is checked on startup.

## Local startup and authentication

Run from your cloned project folder (for example, `webull-trading-desk`):

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

The server binds **127.0.0.1 only**. Do not expose it to the internet or change the
bind address without TLS, firewalling, authentication hardening, and security review.
The single backend key grants full administrative API access; no multi-user RBAC
is implemented. Interactive API documentation is a reference, not a trading UI.

In another PowerShell window, load the key into a variable **without displaying it**:

```powershell
$key = (& .\.venv\Scripts\python.exe -c "from dotenv import dotenv_values; print(dotenv_values('.env')['TRADING_API_KEY'])").Trim()
$headers = @{ 'X-API-Key' = $key }
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/health"
Invoke-RestMethod "$base/v1/system" -Headers $headers
Invoke-RestMethod "$base/v1/broker/connect" -Method Post -Headers $headers
Invoke-RestMethod "$base/v1/portfolio" -Headers $headers
```

If 2FA is enabled, connection initialization can wait up to five minutes for App
approval. Leave 2FA enabled. No broker connection is initiated at server startup.
`connected` indicates a successfully initialized connection, not continuous
verification of remote service availability.

Never send `WEBULL_APP_SECRET` to this HTTP API. `.env`, tokens, databases, and
backups are local sensitive files and are ignored by Git, **not encrypted**. Protect
them with Windows file permissions and disk encryption. Do not log the key or
include it in a URL. The personal backend reads `.env` directly; inherited process
environment values do not override it. Restart the backend after changes. The
separate legacy CLI still permits process-environment overrides.

## Endpoints

Direct API clients use `X-API-Key` for `/v1` endpoints. The local dashboard instead
uses its automatically initialized HttpOnly session plus the CSRF header; no
API key is entered into or sent to the browser.

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Public liveness, no brokerage request |
| GET | `/v1/system` | Environment, connection, selected account, halt state |
| POST | `/v1/broker/connect` | Initialize/validate selected Individual Cash account |
| GET | `/v1/broker/accounts` | Broker account list |
| GET | `/v1/portfolio` | Broker balances and positions |
| GET | `/v1/broker/open-orders` | All broker open-order pages, including external orders |
| GET | `/v1/broker/order-history` | One history page; optional `pagination_key` |
| GET | `/v1/broker/orders/{client_order_id}` | Read a broker order directly |
| GET | `/v1/market/quotes?symbols=AAPL,MSFT` | Batch HTTP snapshots, up to 100 symbols |
| GET | `/v1/market/bars/AAPL?timespan=M1&count=100` | Historical bars, up to 1,200 |
| GET / PUT | `/v1/watchlist` | Persistent local symbol list |
| GET | `/v1/risk` | Limits and kill switch |
| POST | `/v1/risk/check` | Fresh pre-trade check, without submitting |
| PUT | `/v1/risk/kill-switch` | Explicitly enable or halt trading |
| POST | `/v1/orders` | Persist an **offline draft**; `Idempotency-Key` required |
| GET | `/v1/orders?limit=100&offset=0` | Paginated local ledger |
| GET | `/v1/orders/{id}` | Local order and reconciliation state |
| POST | `/v1/orders/{id}/preview` | Risk check + Webull cost preview; no submission |
| POST | `/v1/orders/{id}/submit` | Explicit submission, sandbox only |
| PATCH | `/v1/orders/{id}` | Replace quantity and limit, sandbox only |
| POST | `/v1/orders/{id}/cancel` | Cancel this local order, sandbox only |
| POST | `/v1/orders/{id}/reconcile` | Fetch broker detail and update local state |
| POST | `/v1/reconcile` | Reconcile up to five active local orders |
| GET | `/v1/audit?after=0&limit=100` | Cursor-paginated audit events |
| GET | `/v1/reports/orders` | Local order counts and UTC submission attempts |

The local watchlist is not synced to the Webull app. Financial P&L comes from broker
portfolio responses; local reports do not fabricate returns, fees, or realized P&L.
Cancelled drafts appear in the local ledger without having been broker orders.

## Draft → preview → submit

**The following first block does not place an order.** It creates and previews a
one-share sandbox draft using a returned ask as an example test limit. Review the
symbol, quantity, price, account, and risk results yourself; this is not advice.

```powershell
$quote = @(Invoke-RestMethod "$base/v1/market/quotes?symbols=AAPL" -Headers $headers)[0]
$body = @{
    symbol = 'AAPL'
    side = 'BUY'
    quantity = 1
    limit_price = [string]$quote.ask
    order_type = 'LIMIT'
    time_in_force = 'DAY'
    trading_session = 'CORE'
} | ConvertTo-Json
$orderHeaders = @{ 'X-API-Key' = $key; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$draft = Invoke-RestMethod "$base/v1/orders" -Method Post -Headers $orderHeaders -ContentType 'application/json' -Body $body
Invoke-RestMethod "$base/v1/orders/$($draft.id)/preview" -Method Post -Headers $headers
```

An order is never submitted by creating or previewing it. Risk previews ignore the
halt flag so they can be used while halted; **submit/replace enforce it**.
Submissions always rerun risk checks using fresh broker data.

### Deliberate sandbox submission

**Only run this block when you intend to send the reviewed sandbox order.** A limit
near the quote can fill. Check `/v1/system` first; production mutations are blocked.

```powershell
$enable = @{ enabled = $false; reason = 'Reviewed sandbox trading session' } | ConvertTo-Json
Invoke-RestMethod "$base/v1/risk/kill-switch" -Method Put -Headers $headers -ContentType 'application/json' -Body $enable
Invoke-RestMethod "$base/v1/orders/$($draft.id)/submit" -Method Post -Headers $headers
Invoke-RestMethod "$base/v1/orders/$($draft.id)" -Headers $headers
```

Successful submission/replace/cancel routes return HTTP **202**, not a guarantee of
execution. Inspect `state`, `pending_action`, `filled_quantity`, and `last_error`.
The broker's acknowledgement is reconciled with one immediate detail lookup, then
read-only background updates while the server is running.

### Replacement / cancellation

Replacement accepts a **new total quantity**, not a remaining quantity. Quantity
must exceed the quantity already filled. It reruns risk checks and is blocked
while halted or another mutation is pending.

```json
{"quantity": 1, "limit_price": "YOUR_REVIEWED_PRICE"}
```

Send that numeric-price JSON to `PATCH /v1/orders/{id}`. Cancel with
`POST /v1/orders/{id}/cancel`. Cancellation is available while halted and only
targets orders in this backend's ledger, not unrelated broker/app/CLI orders.
Repeated cancellation of an already cancelled/terminal order does not send again.

## Risk controls

Defaults are intentionally conservative and editable through environment settings
before a restart. Prices/financial calculations use `Decimal`, not binary floats.

| Setting | Default |
|---|---:|
| `TRADING_MAX_ORDER_QUANTITY` | 100 whole shares |
| `TRADING_MAX_ORDER_NOTIONAL` | $10,000 |
| `TRADING_MAX_SYMBOL_EXPOSURE` | $20,000 gross |
| `TRADING_MAX_PORTFOLIO_EXPOSURE` | $100,000 gross |
| `TRADING_MAX_OPEN_ORDERS` | 20 |
| `TRADING_MAX_DAILY_ORDERS` | 50 submission attempts, UTC day |
| `TRADING_MAX_DAILY_LOSS` | $1,000 broker day loss; blocks new buys |
| `TRADING_MAX_PRICE_DEVIATION` | 0.10 (10% from current ask/bid) |
| `TRADING_MAX_QUOTE_AGE_SECONDS` | 120 |
| `TRADING_FEE_BUFFER` | 0.01 (1% estimated buying-cost buffer) |
| `TRADING_RECONCILE_INTERVAL` | 15 seconds; 0 disables the worker |

Buys require sufficient USD settled cash/buying power. Pending buy orders count
toward exposure; pending sells reserve held shares. Options, non-USD positions,
unbounded external buy orders, incomplete pagination, invalid risk fields, and
unknown submission outcomes fail closed. CORE trading requires a fresh regular-
hours quote. This is not a holiday/session calendar engine or a guarantee that
the market remains open between checking and submission.

Risk uses broker snapshots and conservative local reservations. It does not
atomically control external orders or other applications sharing the account.
Do not concurrently trade this account with other applications when relying on
these controls. The 1% fee buffer is an estimate, not a fee guarantee.

## Idempotency and uncertain outcomes

- One `Idempotency-Key` represents one immutable original order intent.
- Repeating draft creation with the same normalized body/key returns that record.
- Reusing a key for different input returns **409**.
- The broker `client_order_id` is generated and persisted **before** submission.
- Repeating `/submit` for a non-draft never resubmits it.
- No automatic submission, replacement, or cancellation retries occur.
- Timeouts, 5xx, interrupted processes, and unrecognized acknowledgements may mean
  the broker already accepted a mutation. They remain uncertain until reconciled.
- Do not create a new order with a new key to work around an uncertain result.
- A missing order in open orders is not proof that it never existed: it may have filled.

Local states include `DRAFT`, `SUBMITTING`, `ACKNOWLEDGED`, `OPEN`,
`PARTIALLY_FILLED`, `MODIFYING`, `CANCELLING`, `FILLED`, `CANCELLED`, `REJECTED`,
`EXPIRED`, and `UNKNOWN`. Pending modifications do not claim a changed limit until
broker detail confirms it. Fill quantities are validated and cannot silently regress.

An unresolved `UNKNOWN` can block new risk indefinitely. If broker detail never
becomes available, verify full order/activity history and resolve with Webull
support/operator review. **There is intentionally no unsafe “forget unknown order”
API or blind resubmit button.** This is one reason the system is not production-certified.

## Kill switch, restarts, and shutdown

Every server startup forces the persisted kill switch **on**, including after a
crash. Interrupted durable mutation intents become `UNKNOWN`. Connecting does not
enable trading. Only an explicit authenticated enable request does.

To halt:

```powershell
$halt = @{ enabled = $true; reason = 'Operator requested halt' } | ConvertTo-Json
Invoke-RestMethod "$base/v1/risk/kill-switch" -Method Put -Headers $headers -ContentType 'application/json' -Body $halt
```

Halting blocks future submissions/replacements. It **does not automatically cancel
open orders, undo in-flight requests, close positions, or guarantee prevention of
fills**. Cancel known active orders explicitly and verify broker status. Broker-side
DAY/GTC orders can remain active after the server shuts down. Stop the local server
with Ctrl+C; the read-only worker shuts down without submitting new orders.

## Rate limits and errors

Authenticated ordinary requests are limited to 120/minute. Emergency halt/cancel
routes bypass this HTTP quota; broker cancellation quotas still apply. The gateway
enforces conservative per-endpoint sandbox quotas (30/60s) and production's stricter
account/order-read quotas. Other scripts using the same Webull App Key share the
broker's quota but are not coordinated with this process's limiter.

- **401:** backend key missing/incorrect.
- **403:** production mutation blocked.
- **409:** idempotency conflict, risk rejection, or invalid/pending state.
- **422:** invalid request/unsupported order type or missing idempotency key.
- **429:** local API/broker quota or Webull throttling; retry reads later.
- **502:** broker failure/schema issue; review mutations before doing anything else.
- **503:** broker is not connected.

Errors include a code, message, optional risk details, and a request ID. Request
headers include `X-Request-ID` and `Cache-Control: no-store`. The SDK adapter does
not expose credentials or raw SDK exception text. The audit API is append-only at
the application layer, not a cryptographically tamper-proof/compliance archive.

## Backup and recovery

```powershell
.\.venv\Scripts\python.exe manage.py backup
```

This uses SQLite's consistent backup API, including while the database is active,
and writes a new file under `data/backups/`. It refuses to overwrite an existing
destination or the active database. Protect backups as sensitive financial records.
To restore, stop the server and use a verified backup matching the same broker
environment/account; reconcile against broker activity before explicitly enabling
trading. Do not delete a ledger to bypass an unresolved mutation.

No automatic audit retention, encryption, migrations beyond schema v1, disaster-
recovery orchestration, distributed execution, or production compliance tooling
is implemented. Review those before considering any future live deployment.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check trading manage.py tests
```

Automated tests never submit to Webull: the complete lifecycle, fill transitions,
timeouts, idempotency, concurrent submissions, risk, audit, restarts, quotas, and
production guards use a fake broker/mock transport and temporary databases.
Real sandbox validation during development is restricted to connection/read/preview
requests. Actual order execution through this new HTTP backend requires separate,
explicit operator sandbox testing.
