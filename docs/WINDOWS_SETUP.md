# Set up on a fresh Windows PC

The public repository contains **source code only**. It does not include `.env`,
Webull credentials, authentication tokens, virtual environments, databases,
backups, logs, or personal trading history. Obtain your own credentials locally.

## 1. Install prerequisites

Install **Git for Windows** and **Python 3.11-3.14** (3.14 recommended). Select
**Add Python to PATH** in the Python installer, then open a new PowerShell window.
A current Edge or Chrome browser is enough for the dashboard; **Node.js is not
required** to run it.

Official downloads:

- Git: https://git-scm.com/downloads/win
- Python: https://www.python.org/downloads/windows/

Alternatively, install with Windows Package Manager:

```powershell
winget install --id Git.Git --exact --source winget
winget install --id Python.Python.3.14 --exact --source winget
```

Reopen PowerShell after installation and check:

```powershell
git --version
python --version
```

If `python` resolves to the Microsoft Store alias instead of your installed
interpreter, use its full path with the setup script's `-Python` option below.

## 2. Clone and install

```powershell
git clone https://github.com/rajashish147/webull-trading-desk.git
cd webull-trading-desk
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

The execution-policy override applies to that **one PowerShell process**, not a
permanent system policy change. Review `setup.ps1` before running downloaded code.
It creates `.venv`, installs runtime dependencies, and copies `.env.example` to
`.env` only if `.env` does not already exist. It does not overwrite existing
credentials or submit broker orders.

For development and tests, use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Dev
```

For a specific Python installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Python "C:\path\to\python.exe"
```

### Manual alternative

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Only run the copy command when `.env` does not exist; otherwise it could overwrite
your locally configured file. Virtual-environment activation is not necessary.

## 3. Add your own environment settings

Open `.env` in your local editor. Fill in the **official sandbox** App Key and App
Secret. Keep:

```dotenv
WEBULL_ENV=sandbox
WEBULL_ENABLE_LIVE_TRADING=false
```

Do not paste keys into GitHub, chat, screenshots, issues, or source files. The blank
`.env.example` is safe to commit; `.env` is excluded by `.gitignore`. Git ignores
are not encryption, so protect the file with Windows permissions and disk encryption.

Then provision the local backend:

```powershell
.\.venv\Scripts\python.exe manage.py init
```

This reads your sandbox account list, sets `WEBULL_ACCOUNT_ID` to the unique
**Individual Cash** account, and generates `TRADING_API_KEY` locally if missing.
It preserves your App Key and App Secret. **No order is placed.** If 2FA is enabled,
approve the request in the Webull App. If account selection is ambiguous, stop and
configure the correct account explicitly rather than guessing.

## 4. Start the dashboard

```powershell
.\.venv\Scripts\python.exe manage.py serve
```

Open **http://127.0.0.1:8000** and click **Connect Webull**. No browser API-key entry
or login is required. The backend reads `.env` directly; edit it locally and restart
the server to apply changes.

Every startup is **halted**. Creating a draft or connecting does not submit an
order. Explicitly review and enable sandbox trading only when you intend to submit
an order. Production broker mutations remain disabled.

If port 8000 is occupied:

```powershell
.\.venv\Scripts\python.exe manage.py serve --port 8001
```

Then open http://127.0.0.1:8001. Do not start two backend processes sharing the same
database; the application refuses a second owner. Stop the server with Ctrl+C.

## 5. Test the installation

Install development requirements if you used runtime-only setup:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check trading manage.py tests
.\.venv\Scripts\python.exe -m pip check
```

Automated tests use fake brokers/mock transport and temporary databases. They do
not need real Webull credentials and never submit orders to Webull.

## Moving from another PC

A source-only clone starts a **new local ledger**; it does not migrate old order
records or audit history. Broker balances/positions and broker history remain at
Webull and are available after connecting. Existing broker orders can continue
after the old server shuts down, but the new local ledger cannot mutate orders
created elsewhere.

Before switching PCs, reconcile uncertain submissions and review/cancel any
unwanted active broker orders on the old PC. Do not delete an old ledger or create
new orders to bypass unknown outcomes. If you later need historical database
migration, use a verified private backup matching the same account/environment—
**never upload it to the public repository**. Do not copy authentication tokens or
the old `.venv`; generate a fresh local session and environment on the new PC.

## Updating the code later

Stop the server before updating:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py serve
```

Your `.env` and ignored local data remain local. Review code changes and migration
notes before using an updated execution workflow. See [API.md](API.md) for risk,
order-state, backup, and operating details.
