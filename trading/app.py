import asyncio
from collections import deque
from contextlib import asynccontextmanager
import hmac
import logging
import re
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from filelock import FileLock, Timeout

from trading.broker import WebullGateway
from trading.config import AppConfig
from trading.database import Database, now
from trading.models import DomainError, HaltRequest, OrderSpec, ReplaceOrder, WatchlistRequest
from trading.service import TradingService
from trading.ui import LocalUI
from webull_cli import Timespan, symbol as validate_symbol

logger = logging.getLogger(__name__)


class RequestLimiter:
    def __init__(self, maximum):
        self.maximum, self.calls, self.lock = maximum, deque(), threading.Lock()

    def check(self):
        with self.lock:
            timestamp = time.monotonic()
            while self.calls and self.calls[0] <= timestamp - 60:
                self.calls.popleft()
            if len(self.calls) >= self.maximum:
                raise DomainError("api_rate_limit", "Local API request quota reached; retry later.", 429)
            self.calls.append(timestamp)


def create_app(config=None, broker=None):
    config = config or AppConfig.from_env()
    database = Database(config.database, config.identity)
    broker = broker or WebullGateway(config.broker)
    service = TradingService(config, database, broker)
    limiter = RequestLimiter(config.request_limit)
    stop = asyncio.Event()
    ui = LocalUI()

    async def worker():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.reconcile_interval)
            except asyncio.TimeoutError:
                if broker.connected:
                    try:
                        await asyncio.to_thread(service.reconcile_active, 5)
                    except Exception:
                        logger.warning("Background read-only reconciliation failed; no orders retried.")

    @asynccontextmanager
    async def lifespan(app):
        config.database.parent.mkdir(parents=True, exist_ok=True)
        process_lock = FileLock(str(config.database) + ".server.lock")
        try:
            process_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("Another backend owns this database; run a single server worker.") from exc
        task = None
        try:
            database.initialize()
            stop.clear()
            if config.reconcile_interval:
                task = asyncio.create_task(worker())
            yield
        finally:
            stop.set()
            if task:
                await task
            process_lock.release()

    app = FastAPI(
        title="Webull Trading API", version="1.0.0", lifespan=lifespan,
        description="Personal dashboard and API for cash-only US equity/ETF trading. Sandbox mutations only.",
    )
    app.state.config, app.state.database, app.state.broker, app.state.service = config, database, broker, service
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authenticate(request: Request, key=Depends(key_header)):
        authenticated = hmac.compare_digest(key.encode(), config.api_key.encode()) if key else ui.authorize(request)
        if not authenticated:
            raise HTTPException(401, "Valid X-API-Key header required.")
        # Halting and emergency cancellation cannot be throttled by ordinary requests.
        emergency = request.url.path == "/v1/risk/kill-switch" or request.url.path.endswith("/cancel")
        if not emergency:
            limiter.check()

    @app.middleware("http")
    async def correlation(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith(("/static/", "/ui/")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(status_code=error.status, content={
            "error": {"code": error.code, "message": str(error), "details": error.details},
            "request_id": getattr(request.state, "request_id", None),
        })

    @app.get("/health", tags=["System"])
    def health():
        return {"status": "ok", "service": "webull-trading-api", "version": "1.0.0"}

    auth = [Depends(authenticate)]

    @app.get("/v1/system", dependencies=auth, tags=["System"])
    def system():
        return {
            "environment": config.broker.environment, "broker_endpoint": config.broker.endpoint,
            "connected": broker.connected, "selected_account": broker.account_summary,
            "production_mutations_enabled": False, "kill_switch": database.state("halt"),
            "reconcile_interval_seconds": config.reconcile_interval,
            "execution_mode": "manual", "schema_version": 1,
            "configuration_source": ".env",
            "credentials_configured": bool(config.broker.app_key and config.broker.app_secret),
        }

    @app.post("/v1/broker/connect", dependencies=auth, tags=["Broker"])
    def connect():
        with service.lock:
            result = broker.connect()
            database.audit_event("broker_connected", details={"environment": config.broker.environment})
            return result

    @app.get("/v1/broker/accounts", dependencies=auth, tags=["Broker"])
    def accounts():
        return broker.accounts()

    @app.get("/v1/portfolio", dependencies=auth, tags=["Portfolio"])
    def portfolio():
        with service.lock:
            return {"timestamp": now(), "balance": broker.balance(), "positions": broker.positions()}

    @app.get("/v1/broker/open-orders", dependencies=auth, tags=["Broker"])
    def open_orders():
        return {"data": broker.open_orders()}

    @app.get("/v1/broker/order-history", dependencies=auth, tags=["Broker"])
    def history(pagination_key: str | None = Query(None, max_length=2000)):
        return broker.history(pagination_key)

    @app.get("/v1/broker/orders/{client_order_id}", dependencies=auth, tags=["Broker"])
    def broker_order(client_order_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", client_order_id):
            raise DomainError("invalid_order_id", "Expected a broker client order ID.", 422)
        return broker.detail(client_order_id)

    def ticker(value):
        try:
            return validate_symbol(value)
        except Exception as exc:
            raise DomainError("invalid_symbol", "Invalid US stock/ETF symbol.", 422) from exc

    @app.get("/v1/market/quotes", dependencies=auth, tags=["Market data"])
    def quotes(symbols: str = Query(..., min_length=1, max_length=2100)):
        items = symbols.split(",")
        if len(items) > 100:
            raise DomainError("too_many_symbols", "At most 100 symbols per request.", 422)
        return broker.quotes(list(dict.fromkeys(ticker(item) for item in items)))

    @app.get("/v1/market/bars/{symbol}", dependencies=auth, tags=["Market data"])
    def bars(symbol: str, timespan: str = "M1", count: int = Query(100, ge=1, le=1200)):
        if timespan not in {item.name for item in Timespan}:
            raise DomainError("invalid_timespan", "Invalid Webull candle timespan.", 422)
        return broker.bars(ticker(symbol), timespan, count)

    @app.get("/v1/watchlist", dependencies=auth, tags=["Watchlist"])
    def watchlist():
        return {"symbols": database.state("watchlist")}

    @app.put("/v1/watchlist", dependencies=auth, tags=["Watchlist"])
    def update_watchlist(request: WatchlistRequest):
        symbols = list(dict.fromkeys(request.symbols))
        database.set_state("watchlist", symbols, "watchlist_changed")
        return {"symbols": symbols}

    @app.get("/v1/risk", dependencies=auth, tags=["Risk"])
    def risk_config():
        return {
            "kill_switch": database.state("halt"),
            "limits": {key: str(value) for key, value in vars(config.limits).items()},
            "cash_only": True, "short_selling_enabled": False,
        }

    @app.post("/v1/risk/check", dependencies=auth, tags=["Risk"])
    def check_risk(spec: OrderSpec):
        with service.lock:
            return service.risk.check(spec, ignore_halt=True)

    @app.put("/v1/risk/kill-switch", dependencies=auth, tags=["Risk"])
    def halt(request: HaltRequest):
        return service.set_halt(request.enabled, request.reason)

    @app.post("/v1/orders", status_code=201, dependencies=auth, tags=["Orders"])
    def create_order(spec: OrderSpec, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128 or not key.isascii() or any(ord(character) < 33 for character in key):
            raise DomainError("idempotency_key_required", "Provide an ASCII Idempotency-Key header (1-128 non-space characters).", 422)
        return service.create(spec, key)

    @app.get("/v1/orders", dependencies=auth, tags=["Orders"])
    def orders(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {"data": database.orders(limit, offset), "limit": limit, "offset": offset}

    @app.get("/v1/orders/{order_id}", dependencies=auth, tags=["Orders"])
    def get_order(order_id: str):
        return database.get(order_id)

    @app.post("/v1/orders/{order_id}/preview", dependencies=auth, tags=["Orders"])
    def preview(order_id: str):
        return service.preview(order_id)

    @app.post("/v1/orders/{order_id}/submit", status_code=202, dependencies=auth, tags=["Orders"])
    def submit(order_id: str):
        return service.submit(order_id)

    @app.patch("/v1/orders/{order_id}", status_code=202, dependencies=auth, tags=["Orders"])
    def replace_order(order_id: str, request: ReplaceOrder):
        return service.replace(order_id, request)

    @app.post("/v1/orders/{order_id}/cancel", status_code=202, dependencies=auth, tags=["Orders"])
    def cancel_order(order_id: str):
        return service.cancel(order_id)

    @app.post("/v1/orders/{order_id}/reconcile", dependencies=auth, tags=["Orders"])
    def reconcile_order(order_id: str):
        return service.reconcile(order_id)

    @app.post("/v1/reconcile", dependencies=auth, tags=["Orders"])
    def reconcile_all():
        return {"data": service.reconcile_active(5)}

    @app.get("/v1/audit", dependencies=auth, tags=["Audit"])
    def audit(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return {"data": database.audit(after, limit)}

    @app.get("/v1/reports/orders", dependencies=auth, tags=["Reports"])
    def report():
        return database.report()

    ui.mount(app)
    return app
