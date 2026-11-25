# API testing console (Postman-style)

Open **http://127.0.0.1:8000/api-console**, or click **API testing console** in the
dashboard sidebar. It uses the **same running backend, .env, accounts, ledgers,
risk limits and live safeguards**. It is not a separate trading engine.

The console does not fetch your raw `.env` or expose Webull/backend keys. The
Python server reads them, and the browser uses the existing automatic local
HttpOnly session plus CSRF protection. No key-entry screen is needed.

## What you can inspect

- Searchable endpoint catalog generated from the backend's OpenAPI schema.
- Editable HTTP method and local API path.
- Path parameters, query parameters and custom JSON headers.
- Editable JSON body and JSON formatting.
- Request preview showing the method, final URL, environment, headers and body.
- Response status, duration, byte count, formatted JSON, raw text and browser-visible headers.
- **Executed request**: the frozen request that produced that response, even if you
  have since edited the next request.
- Redacted, memory-only history; clicking an entry loads it but never runs it.
- Redacted response download and a cURL template with credential placeholders.

This console executes your **local project API** (`/v1/...`, `/health`, and
`/openapi.json`), not arbitrary remote URLs or raw Webull endpoints. SDK signing,
broker credentials and execution controls remain in Python. There are no
automatic request runners, broker mutation retries or response scripts.

## Basic workflow

1. Choose an endpoint from **Endpoints**, or type a local path yourself.
2. Verify the **HTTP method** and **Request environment**.
3. Fill required path parameters under **Params**.
4. Edit query rows; unchecked rows are not sent.
5. Add custom headers under **Headers**, if required.
6. Edit/format the JSON under **Body**. Replace `REVIEW_PRICE` yourself.
7. Inspect **Request preview**.
8. Click **Send request**.
9. Inspect **Pretty**, **Raw**, **Headers**, and **Executed request**.

GET/HEAD requests never send an editor body. Invalid JSON or unfilled path
parameters are blocked locally before execution. Redirects are not automatically
followed, preventing a redirect from replaying an action; use canonical paths,
normally without a trailing slash.

Only session, schema and system metadata are loaded automatically. Manual
requests execute only when you press Send/confirm. Read-only `GET /v1/system`
refreshes the displayed context before/after a manual request; it does not connect
the broker, arm trading, or create an order.

## Useful requests

| Task | Request | Input |
|---|---|---|
| View current context | `GET /v1/system` | None |
| Connect the selected broker | `POST /v1/broker/connect` | No body |
| View balances/positions | `GET /v1/portfolio` | None |
| Fetch quotes | `GET /v1/market/quotes` | Query `symbols=AAPL,MSFT` |
| Fetch bars | `GET /v1/market/bars/{symbol}` | Path symbol; query `timespan=M1`, `count=60` |
| Check pre-trade risk | `POST /v1/risk/check` | Order JSON; does not submit |
| Create offline draft | `POST /v1/orders` | Order JSON plus `Idempotency-Key` |
| Read local orders | `GET /v1/orders` | Optional limit/offset |
| Read one order | `GET /v1/orders/{order_id}` | Local order ID |
| Preview a draft | `POST /v1/orders/{order_id}/preview` | No body; does not submit |
| Reconcile an order | `POST /v1/orders/{order_id}/reconcile` | No body; read-only broker lookup |
| Request submission | `POST /v1/orders/{order_id}/submit` | Explicit broker action; all safeguards apply |
| Request replacement | `PATCH /v1/orders/{order_id}` | New total quantity and limit |
| Request cancellation | `POST /v1/orders/{order_id}/cancel` | Explicit broker action; verify final state |
| Halt all profiles | `PUT /v1/risk/kill-switch` | `{"enabled":true,"reason":"Operator halt"}` |

Symbols/examples above illustrate requests, not trade recommendations. Prices are
not supplied by the examples: choose and review your own intended price.

## Environment selection is explicit

The top badge shows the **actual active server profile**. **Request environment**
controls `X-Trading-Environment` on the request; changing it does **not** switch
the server. A mismatched header normally returns **409**, which is useful when
testing stale-tab/context protection. Global halt deliberately remains available.

**Follow active profile for new requests** updates the request environment when
context is refreshed. A loaded history entry instead pins its original environment
and turns that checkbox off. It is never silently retargeted into another account.

To switch the server yourself, execute `PUT /v1/environment` with the **currently
active** request environment header and a body such as:

```json
{"environment":"production","confirmation":"SWITCH TO LIVE"}
```

This control requires additional console review. It halts/disarms all profiles and
is blocked while active/uncertain orders exist. To switch back, target `sandbox`
with `SWITCH TO SANDBOX`. Inspect the returned context before the next action.

## Headers and captured order IDs

Custom headers are a JSON object, for example:

```json
{"Idempotency-Key":"your-unique-intent-id"}
```

The console generates an idempotency key when you select draft creation, and the
**Generate idempotency key** button can deliberately generate another. A retry
does not silently regenerate it. Do not create a new intent to bypass an uncertain
broker outcome.

**Use response order IDs** fills matching path fields from a response captured in
the chosen environment. It does not execute a request. You can always enter IDs
manually. Backend account/environment scope is still verified.

Auth/CSRF/cookie/environment/host headers are managed separately and cannot be
overridden in the custom editor. Never paste `.env` keys here. To deliberately
inspect missing-auth responses, turn off **Automatic local-session auth**; the
manual request omits cookies and CSRF, so protected routes return 401. Turn it back
on afterward.

## Sandbox and live broker actions

Clicking Send for submission/replacement/cancellation opens a review dialog.
Sandbox broker actions require the review checkbox. Live broker actions also
require typing **EXECUTE LIVE REQUEST** in the console's review dialog.

This extra console review does **not replace** backend live safeguards:

1. Production profile credentials/account must be configured and connected.
2. Submission/replacement requires the local `.env` opt-in and an unexpired live arm.
3. Obtain an exact approval from `POST /v1/orders/{id}/live-approval`.
4. Supply `X-Live-Confirmation` and `X-Live-Acknowledgement` matching that approval.
5. The backend reruns checks and verifies the one-use approval before sending once.

The current approval response can show its short-lived token so you can inspect
and use it. **Use live approval headers** inserts the token and exact
acknowledgement into the editor locally; it does not send anything. Headers remain
available when selecting a matching endpoint, but are cleared after an execution
attempt. Get a fresh approval rather than blindly reusing an old one.

Approvals expire after 60 seconds and are bound to the operation, order revision,
parameters, account and environment. Cancellation works disarmed but still needs
its own confirmation. Arming requires the backend phrase `ENABLE LIVE TRADING`;
the console adds a separate explicit review before that control request.

HTTP 202 does not guarantee a fill or successful cancellation. Inspect `state`,
`pending_action`, filled quantity and errors, then reconcile. A transport failure
may mean the broker already processed the request. The console never retries a
broker action automatically.

See [LIVE_TRADING.md](LIVE_TRADING.md) for the complete live workflow and limitations.

## History, output and exports

- The latest response is shown as plain text/JSON, never executed as HTML/script.
- Up to 20 entries are kept **in page memory only**; reload/close clears them.
- Tokens, secret/header fields and sensitive query values are redacted in history.
- Clicking a history entry restores its request and redacted response, **not execution**.
- Redacted history cannot supply a usable live token; get a fresh approval.
- Downloads are redacted but may still contain financial data/account/order IDs.
  Treat them as private and do not upload them to GitHub/issues indiscriminately.
- cURL templates use POSIX/Git Bash quoting and placeholders for the backend key
  and live token; they do not export the browser session or `.env` secrets. They
  are not automatically executed and are not PowerShell-native scripts.
- Browser header views omit headers the browser does not expose, including HttpOnly
  Set-Cookie values. These are the local API's headers, not raw broker signing headers.

## Validation and limitations

Python tests cover local serving, credential exclusion, session authorization and
existing trade safeguards. Pure JavaScript tests cover request construction,
redaction, templates, URL/header restrictions and live review classification:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/console_core.test.mjs
```

Node is optional for development tests, **not required to run the console**.
The console shares your real running backend: use sandbox for exploration and
never assume "testing frontend" means a broker action is simulated.
