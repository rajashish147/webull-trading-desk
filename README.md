# Webull Desk — personal trading dashboard

A simple local dashboard backed by **FastAPI + SQLite** and Webull's official Python
SDK. Credentials are read directly from your project’s `.env` file on the Python
server. **No API-key entry or login is needed in the local UI.**

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
- Production brokerage mutations are **hard-disabled** in this backend.

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

### Scope and limitations

This is a **sandbox-first manual execution backend**, not a production-certified
brokerage platform. It supports **whole-share LIMIT BUY/SELL orders, DAY/GTC, CORE
session**, and a single Individual Cash account. Market/stop orders, fractional
shares, shorting, options, strategy automation, backtesting, MQTT/gRPC streaming,
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
```

Tests use mocked brokerage transport and temporary databases. For runtime only,
install `requirements.txt`; the development file adds the HTTP test client and lint tool.

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

## 5. Production / live trading (later)

Sandbox keys cannot be assumed to work in production. Obtain production access
and production keys through Webull's **Developer Console → Access Credentials →
API Keys**. An opened brokerage account is required for the retail production
application; Webull reviews access. Set up market-data subscriptions separately.

To read your live account later, replace the local credentials and account ID and
set `WEBULL_ENV=production`, **keeping `WEBULL_ENABLE_LIVE_TRADING=false`**.
Then run `status`, `accounts`, and other read-only commands first.

Production order submission is blocked unless **all three** are explicitly set:

1. `.env`: `WEBULL_ENABLE_LIVE_TRADING=true`
2. Order command: `--submit`
3. Order command: `--confirm-live-order`

Without `--submit`, `order` remains offline even in production. These CLI guards
are not broker-level permissions and do not protect a different script using the
SDK directly. Leave live trading disabled until you have reviewed permissions,
account selection, order parameters, and tested your workflow.

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
