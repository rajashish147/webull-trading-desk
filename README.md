# Webull Desk — personal trading dashboard

A simple local dashboard backed by **FastAPI + SQLite** and Webull's official Python
SDK. Credentials are read directly from your project’s `.env` file on the Python
server. **No API-key entry or login is needed in the local UI.**

## Sandbox and controlled live trading

Version 2 supports **separate sandbox/live credentials, clients, ledgers, and risk
limits**. Sandbox remains the recommended startup profile. Live is **disabled by
default** and requires production credential/account validation, a short-lived
typed arm, and an exact one-use confirmation for each broker action.

Read **[the live-trading operating guide](docs/LIVE_TRADING.md)** before using a
real account. This is a personal, supervised cash-equity execution tool, **not a
broker-certified or guaranteed production platform**. All development execution
tests use mock brokers; no real-money orders are placed by setup or tests.

## Postman-style API testing console

Open **http://127.0.0.1:8000/api-console**, separately from the trading dashboard.
Choose/edit endpoints, methods, path/query parameters, headers and JSON bodies;
inspect the executed request, response status/timing, Pretty/Raw output and headers.
The local session handles authentication automatically; `.env` keys stay in Python.
History is redacted and memory-only. Live arming and one-use approvals are not bypassed.

Read **[the API console guide](docs/API_CONSOLE.md)** before executing broker actions.

## First strategy: two-stock sum

Use **Strategies** for bounded automation: buy both equal quantities when the
ask sum crosses to `Z` or below, then attempt to sell when the bid sum reaches
`Z+a` or `Z-b`. Repeats require cooldown/fresh crossing and finite entry attempts.
Failed/partial legs cancel remaining working orders while authorized, then halt
for manual recovery; fills are not automatically reversed.

Live automation is **separately disabled by default** and requires explicit
policy/window authorization in addition to global live arming. This is not atomic
pair execution, guaranteed profit/stop-loss, or proven production reliability.
Read **[STRATEGIES.md](docs/STRATEGIES.md)** before arming it.

The strategy form automatically loads entered stocks and displays combined ask/bid
sums, share-adjusted cost, fee buffer, target/downside distances and budget warnings.
Refreshes are debounced/quota-protected and never arm or submit an order.

## Already running? Daily-use guide

Read **[the dashboard user guide](docs/USER_GUIDE.md)** for the button-by-button
workflow: data → draft → preview → explicit submission → reconcile → modify/cancel
→ finish safely. It covers operation, not installation.

## Fresh Windows PC

**Credentials and local trading data are not included in this public repository.**

```powershell
git clone https://github.com/rajashish147/webull-trading-desk.git
cd webull-trading-desk
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Fill in `.env` yourself, then run:

```powershell
.\.venv\Scripts\python.exe manage.py init
.\.venv\Scripts\python.exe manage.py serve
```

See the **[fresh Windows setup guide](docs/WINDOWS_SETUP.md)** for prerequisites,
development tests, credential handling, and moving from an old PC.

## Start the dashboard

After configuring your own `.env`, run `manage.py init` to select the Individual
Cash sandbox account and generate a local backend API key. Your Webull App Key
and App Secret are preserved. They are never bundled with the source code.

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

- Dashboard: **`http://127.0.0.1:8000`**
- API reference: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`
- Click **Connect Webull** to load your portfolio and watchlist.
- Use the order ticket to check risk and create an offline draft, then preview or
  explicitly submit it from the order dialog.
- The Orders page supports details, modification, cancellation, and reconciliation.
- Market data includes snapshot quotes and a historical closing-price chart.
- Activity shows broker history and audit events; Settings shows non-secret `.env`
  configuration and your risk limits.
- Browser access is automatic using a loopback-only local session. Direct API
  clients may still use the backend `X-API-Key` from `.env`.
- Every startup is **halted**. Connecting or creating a draft does not trade.
- Use the **Profile** selector to choose sandbox/live. Switching halts all profiles
  and is blocked while active/uncertain orders exist. Stale tabs cannot retarget orders.
- Live orders require `WEBULL_ENABLE_LIVE_TRADING=true`, an explicit temporary arm,
  and per-order typed confirmation for manual actions. Separately armed strategies
  use a reviewed finite delegation. Manual live cancellation remains available while disarmed.

Read [the complete API and operating guide](docs/API.md) before enabling trading.

The backend loads `.env` at startup, ignoring stale inherited shell settings. Edit
the file locally and **restart** to apply changes. Secrets are never served to the
browser; the frontend does not fetch the raw `.env` file. The original CLI retains
its environment-variable override behavior.

### Included

- Explicit broker connection and Individual Cash account validation.
- Portfolio balances, positions, market snapshots, and historical candles.
- Persistent, idempotent drafts; server-side previews; explicit submissions.
- Order lookup, replacement, cancellation, history, and fill reconciliation.
- Conservative cash/long-only, size, exposure, daily loss, price, and quote-age checks.
- Kill switch; emergency cancellation remains available while halted.
- Durable mutation intents, uncertain-outcome protection, and restart recovery.
- Read-only background reconciliation; never automatic order submission/retry.
- Persistent local watchlist, paginated audit trail, local order reports, SQLite backups.
- API-key authentication, per-endpoint broker quotas, request IDs, single-process database ownership.
- One-use live approvals bound to account, environment, order revision, operation,
  quantity and price; approvals expire after 60 seconds.
- Automatic schema-1/2 backup/migration to schema 3 without discarding existing orders.

### Scope and limitations

This is a **sandbox-first, optionally live manual/bounded-strategy execution backend**, not a production-certified
brokerage platform. It supports **whole-share LIMIT BUY/SELL orders, DAY/GTC, CORE
session**, and a single Individual Cash account. Market/stop orders, fractional
shares, shorting, options, backtesting, MQTT/gRPC streaming,
multi-user permissions, cloud deployment, and automated emergency liquidation
are not included. Market data uses HTTP snapshots, not push streaming.

Risk checks use fresh broker snapshots, not an atomic guarantee of broker funds
or exchange execution. Review and exercise the system thoroughly in sandbox.
The API sandbox is separate from the app's Paper Trading portfolio.

### Install / tests

Backend requirements: **Python 3.11–3.14**; this project runs on Python 3.14.7.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
# Provision credentials/account only when needed; no orders are placed:
.\.venv\Scripts\python.exe manage.py init
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check trading manage.py tests
node --test tests/console_core.test.mjs
node --test tests/strategy_preview.test.mjs
```

Tests use mocked brokerage transport and temporary databases. For runtime only,
install `requirements.txt`; the development file adds the HTTP test client and lint tool.
Node is optional for the API-console JavaScript tests; it is not needed to run either UI.

## Original CLI (retained)

Python CLI using **`webull-openapi-python-sdk`**, not the unofficial `webull` package.
Configured for **US sandbox** by default. Install dependencies into a new local
`.venv`; virtual environments and credentials are not distributed with the code.

## 1. Enter your sandbox credentials locally

Edit `.env` in your cloned project folder:

```dotenv
WEBULL_ENV=sandbox
WEBULL_APP_KEY=your_actual_sandbox_app_key
WEBULL_APP_SECRET=your_actual_sandbox_app_secret
WEBULL_ACCOUNT_ID=
WEBULL_ENABLE_LIVE_TRADING=false
```

Use your own credentials, not the placeholder strings above. Never paste keys into
chat, screenshots, source code, or a repository. `.env` and the SDK token cache
`.webull_tokens/` are excluded by `.gitignore`; this is not encryption. Restrict
local access and protect backups. Existing process environment variables override
`.env`, so check those if the environment seems wrong.

## 2. Verify the connection

Run these in PowerShell from your cloned project folder (no virtual-environment
activation or execution-policy change needed):

```powershell
.\.venv\Scripts\python.exe webull_cli.py status
.\.venv\Scripts\python.exe webull_cli.py accounts
```

`status` only checks local configuration; it does **not** verify credentials.
`accounts` connects to Webull. If 2FA is enabled, approve the verification request
in the Webull App; initialization can wait up to five minutes. Keep 2FA enabled.
Copy the correct sandbox `account_id` from the response into `WEBULL_ACCOUNT_ID`.
The starter deliberately never auto-selects an account.

SDK connectivity and a sandbox place/modify/cancel lifecycle were verified during
initial setup. No orders are sent by provisioning or automated development tests.

## 3. Read accounts and market data

```powershell
.\.venv\Scripts\python.exe webull_cli.py balance
.\.venv\Scripts\python.exe webull_cli.py positions
.\.venv\Scripts\python.exe webull_cli.py orders
.\.venv\Scripts\python.exe webull_cli.py quotes AAPL MSFT
.\.venv\Scripts\python.exe webull_cli.py bars AAPL --timespan M1 --count 100
.\.venv\Scripts\python.exe webull_cli.py bars AAPL --timespan D --count 30
```

`quotes` returns snapshots, not an ongoing real-time stream. Historical bars use
the current batch endpoint. `orders` returns one page of **open** orders; if Webull
returns a pagination key, pass it with `orders --pagination-key KEY`. Account
commands accept `--account-id` as an explicit override.

Production US stock/ETF data, including historical data, requires an active
**OpenAPI market-data subscription / Non-Display entitlement**. An app quote
subscription is not a guarantee of API entitlement. Sandbox data availability
depends on your sandbox permissions and may not match production data.

## 4. Sandbox trading

The official OpenAPI sandbox is a separate testing environment, **not a connection
to the Webull app's Paper Trading portfolio**. Treat sandbox fills/data as test
behavior, not evidence of production execution quality.

Plan an order **offline** (no API request, no server validation, no execution):

```powershell
.\.venv\Scripts\python.exe webull_cli.py order AAPL --side BUY --quantity 1 --limit-price 10.00
```

The ticker and price above are only payload examples, not a trade recommendation
or current quote. Review the plan and choose your own test values.

To actually send your reviewed order to the **sandbox**, repeat your command with
`--submit`. Check `status` first to verify the environment. Do not add `--submit`
unless you intend to send an order. The CLI supports whole-share stock/ETF limit
orders, DAY duration, regular trading hours only; not options, fractional shares,
market orders, automated strategies, or order cancellation/replacement.

For submission, pass `--client-order-id` with the ID from the offline plan if you
want to preserve it. The CLI prints the submission ID before sending. The SDK's
automatic retries are disabled. **After any timeout or interruption, check orders
and account activity in Webull before retrying: the order may have succeeded.**
Use the same client order ID when reconciling an ambiguous submission; do not
blindly send a new order. Open orders alone cannot rule out an immediate fill.

## 5. Live trading: use the guarded dashboard/API, not the legacy CLI

Sandbox keys cannot be assumed to work in production. Obtain production access
and production keys through Webull's **Developer Console → Access Credentials →
API Keys**. An opened brokerage account is required for the retail production
application; Webull reviews access. Set up market-data subscriptions separately.

Keep sandbox credentials separate. Use `WEBULL_LIVE_APP_KEY`,
`WEBULL_LIVE_APP_SECRET`, and `WEBULL_LIVE_ACCOUNT_ID`; production never falls back
to generic/sandbox credentials. See [LIVE_TRADING.md](docs/LIVE_TRADING.md) for
read-only validation, pilot limits, arming, approvals, and emergency cancellation.

**Production order submission in the legacy CLI is now always blocked**, even with
its old confirmation flag. Live execution must use the guarded dashboard/API.
Do not use raw SDK calls to bypass the risk/confirmation layer.

## Reinstall and run local tests

Requires Python 3.8–3.14 according to the official SDK. Installed/tested locally
with Python 3.14.7. To recreate this environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

If `.env` does not exist, copy `.env.example` to `.env`. Dependencies are pinned
at the directly used package level, not as a full transitive lockfile. Tests use
mocked authentication/transport and do not call Webull or send orders.

## Troubleshooting

- **Missing key/secret:** edit `.env`; do not include surrounding `<...>` placeholders.
- **401 / signature failure:** check key/secret, sandbox vs production, and Windows clock sync.
- **403:** check API permissions and OpenAPI market-data entitlements. Do not disable 2FA to work around errors.
- **Verification wait:** approve in the Webull App, then retry after expiration if needed.
- **429:** respect Webull's rate limits; no automatic retry is enabled here.
- **Network/TLS failure:** check firewall/proxy and endpoint availability. Certificate verification stays enabled.
- **Unexpected account:** sandbox and production accounts are separate; never reuse an account ID without checking.

## Official references

- [Getting started](https://developer.webull.com/apis/docs/getting-started/)
- [SDKs and environment endpoints](https://developer.webull.com/apis/docs/sdk)
- [Credentials, sandbox, and production application](https://developer.webull.com/apis/docs/authentication/IndividualApplicationAPI/)
- [Authentication and 2FA](https://developer.webull.com/apis/docs/authentication/overview/)
- [Market-data requirements](https://developer.webull.com/apis/docs/market-data-api/getting-started)
- [Official SDK source](https://github.com/webull-inc/webull-openapi-python-sdk)
