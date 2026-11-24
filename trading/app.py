import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import logging
import re
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from trading.config import AppConfig
from trading.database import now
from trading.models import DomainError, EnvironmentRequest, HaltRequest, LiveApprovalRequest, LiveArmRequest, OrderSpec, ReplaceOrder, WatchlistRequest
from trading.profiles import ProfileManager
from trading.safety import LiveConfirmation
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
    runtime = ProfileManager(config, broker)
    limiter = RequestLimiter(config.request_limit)
    stop = asyncio.Event()
    ui = LocalUI()

    async def worker():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.reconcile_interval)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(runtime.reconcile_connected)
                except Exception:
                    logger.warning("Background read-only reconciliation failed; no orders retried.")

    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        task = None
        try:
            stop.clear()
            if config.reconcile_interval:
                task = asyncio.create_task(worker())
            yield
        finally:
            stop.set()
            if task:
                await task
            runtime.stop()

    app = FastAPI(
        title="Webull Trading API", version="2.0.0", lifespan=lifespan,
        description="Personal sandbox/live cash-equity trading with isolated profiles and explicit live approvals.",
    )
    def bind_state():
        profile = runtime.active
        app.state.config, app.state.database, app.state.broker, app.state.service = profile.config, profile.database, profile.broker, profile.service
    bind_state()
    app.state.profile_manager = runtime
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
        return {"status": "ok", "service": "webull-trading-api", "version": "2.0.0"}

    auth = [Depends(authenticate)]

    def system_info():
        profile = runtime.active
        selected, database, broker, service = profile.config, profile.database, profile.broker, profile.service
        armed = service.live_armed()
        halt = dict(database.state("halt"))
        arm = database.state("live_arm") or {}
        if selected.broker.environment == "production" and not armed:
            halt.update(enabled=True, reason="Live trading is disarmed or expired.")
        return {
            "environment": selected.broker.environment, "broker_endpoint": selected.broker.endpoint,
            "connected": broker.connected, "selected_account": broker.account_summary,
            "production_mutations_enabled": armed, "kill_switch": halt,
            "live_execution_configured": selected.broker.environment == "production" and selected.broker.enable_live_trading,
            "live_armed": armed,
            "live_arm_expires_at": datetime.fromtimestamp(arm["expires_at"], timezone.utc).isoformat() if arm.get("expires_at") else None,
            "configured_account_hint": selected.broker.account_id[-4:], "profiles": runtime.summary(),
            "account_fingerprint": selected.identity,
            "reconcile_interval_seconds": selected.reconcile_interval,
            "execution_mode": "manual", "schema_version": 2,
            "configuration_source": ".env",
            "credentials_configured": bool(selected.broker.app_key and selected.broker.app_secret),
        }

    @app.get("/v1/system", dependencies=auth, tags=["System"])
    def system():
        with runtime.lock:
            return system_info()

    @app.get("/v1/environments", dependencies=auth, tags=["System"])
    def environments():
        return {"data": runtime.summary()}

    @app.put("/v1/environment", dependencies=auth, tags=["System"])
    def switch_environment(body: EnvironmentRequest, request: Request):
        with runtime.lock:
            runtime.switch(body, request.headers.get("X-Trading-Environment"))
            bind_state()
            return system_info()

    @app.post("/v1/live/arm", dependencies=auth, tags=["Risk"])
    def arm_live(body: LiveArmRequest, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.arm_live(body.confirmation)

    @app.post("/v1/broker/connect", dependencies=auth, tags=["Broker"])
    def connect(request: Request):
        with runtime.scope(request) as profile:
            result = profile.broker.connect()
            profile.database.audit_event("broker_connected", details={"environment": profile.config.broker.environment})
            return result

    @app.get("/v1/broker/accounts", dependencies=auth, tags=["Broker"])
    def accounts(request: Request):
        with runtime.scope(request) as profile:
            return profile.broker.accounts()

    @app.get("/v1/portfolio", dependencies=auth, tags=["Portfolio"])
    def portfolio(request: Request):
        with runtime.scope(request) as profile:
            snapshot = profile.broker.portfolio() if hasattr(profile.broker, "portfolio") else {"balance": profile.broker.balance(), "positions": profile.broker.positions(), "cached": False}
            return {"timestamp": now(), **snapshot}

    @app.get("/v1/broker/open-orders", dependencies=auth, tags=["Broker"])
    def open_orders(request: Request):
        with runtime.scope(request) as profile:
            return {"data": profile.broker.open_orders()}

    @app.get("/v1/broker/order-history", dependencies=auth, tags=["Broker"])
    def history(request: Request, pagination_key: str | None = Query(None, max_length=2000)):
        with runtime.scope(request) as profile:
            return profile.broker.history(pagination_key)

    @app.get("/v1/broker/orders/{client_order_id}", dependencies=auth, tags=["Broker"])
    def broker_order(client_order_id: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", client_order_id):
            raise DomainError("invalid_order_id", "Expected a broker client order ID.", 422)
        with runtime.scope(request) as profile:
            return profile.broker.detail(client_order_id)

    def ticker(value):
        try:
            return validate_symbol(value)
        except Exception as exc:
            raise DomainError("invalid_symbol", "Invalid US stock/ETF symbol.", 422) from exc

    @app.get("/v1/market/quotes", dependencies=auth, tags=["Market data"])
    def quotes(request: Request, symbols: str = Query(..., min_length=1, max_length=2100)):
        items = symbols.split(",")
        if len(items) > 100:
            raise DomainError("too_many_symbols", "At most 100 symbols per request.", 422)
        with runtime.scope(request) as profile:
            return profile.broker.quotes(list(dict.fromkeys(ticker(item) for item in items)))

    @app.get("/v1/market/bars/{symbol}", dependencies=auth, tags=["Market data"])
    def bars(symbol: str, request: Request, timespan: str = "M1", count: int = Query(100, ge=1, le=1200)):
        if timespan not in {item.name for item in Timespan}:
            raise DomainError("invalid_timespan", "Invalid Webull candle timespan.", 422)
        with runtime.scope(request) as profile:
            return profile.broker.bars(ticker(symbol), timespan, count)

    @app.get("/v1/watchlist", dependencies=auth, tags=["Watchlist"])
    def watchlist(request: Request):
        with runtime.scope(request) as profile:
            return {"symbols": profile.database.state("watchlist")}

    @app.put("/v1/watchlist", dependencies=auth, tags=["Watchlist"])
    def update_watchlist(body: WatchlistRequest, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            symbols = list(dict.fromkeys(body.symbols))
            profile.database.set_state("watchlist", symbols, "watchlist_changed")
            return {"symbols": symbols}

    @app.get("/v1/risk", dependencies=auth, tags=["Risk"])
    def risk_config(request: Request):
        with runtime.scope(request) as profile:
            return {"kill_switch": profile.database.state("halt"),
                    "limits": {key: str(value) for key, value in vars(profile.config.limits).items()},
                    "cash_only": True, "short_selling_enabled": False}

    @app.post("/v1/risk/check", dependencies=auth, tags=["Risk"])
    def check_risk(spec: OrderSpec, request: Request):
        with runtime.scope(request) as profile:
            return profile.service.risk.check(spec, ignore_halt=True)

    @app.put("/v1/risk/kill-switch", dependencies=auth, tags=["Risk"])
    def halt(body: HaltRequest, request: Request):
        if body.enabled:
            return runtime.halt_all(body.reason)
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.set_halt(False, body.reason)

    @app.post("/v1/orders", status_code=201, dependencies=auth, tags=["Orders"])
    def create_order(spec: OrderSpec, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128 or not key.isascii() or any(ord(character) < 33 for character in key):
            raise DomainError("idempotency_key_required", "Provide an ASCII Idempotency-Key header (1-128 non-space characters).", 422)
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.create(spec, key)

    @app.get("/v1/orders", dependencies=auth, tags=["Orders"])
    def orders(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        with runtime.scope(request) as profile:
            return {"data": profile.database.orders(limit, offset), "limit": limit, "offset": offset}

    @app.get("/v1/orders/{order_id}", dependencies=auth, tags=["Orders"])
    def get_order(order_id: str, request: Request):
        with runtime.scope(request) as profile:
            return profile.database.get(order_id)

    @app.post("/v1/orders/{order_id}/preview", dependencies=auth, tags=["Orders"])
    def preview(order_id: str, request: Request):
        with runtime.scope(request) as profile:
            return profile.service.preview(order_id)

    def live_confirmation(request):
        token = request.headers.get("X-Live-Confirmation")
        return LiveConfirmation(token, request.headers.get("X-Live-Acknowledgement", "")) if token else None

    @app.post("/v1/orders/{order_id}/live-approval", dependencies=auth, tags=["Orders"])
    def live_approval(order_id: str, body: LiveApprovalRequest, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.live_approval(order_id, body)

    @app.post("/v1/orders/{order_id}/submit", status_code=202, dependencies=auth, tags=["Orders"])
    def submit(order_id: str, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.submit(order_id, live_confirmation(request))

    @app.patch("/v1/orders/{order_id}", status_code=202, dependencies=auth, tags=["Orders"])
    def replace_order(order_id: str, body: ReplaceOrder, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.replace(order_id, body, live_confirmation(request))

    @app.post("/v1/orders/{order_id}/cancel", status_code=202, dependencies=auth, tags=["Orders"])
    def cancel_order(order_id: str, request: Request):
        with runtime.scope(request, mutation=True) as profile:
            return profile.service.cancel(order_id, live_confirmation(request))

    @app.post("/v1/orders/{order_id}/reconcile", dependencies=auth, tags=["Orders"])
    def reconcile_order(order_id: str, request: Request):
        with runtime.scope(request) as profile:
            return profile.service.reconcile(order_id)

    @app.post("/v1/reconcile", dependencies=auth, tags=["Orders"])
    def reconcile_all(request: Request):
        with runtime.scope(request) as profile:
            return {"data": profile.service.reconcile_active(2 if profile.config.broker.environment == "production" else 5)}

    @app.get("/v1/audit", dependencies=auth, tags=["Audit"])
    def audit(request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        with runtime.scope(request) as profile:
            return {"data": profile.database.audit(after, limit)}

    @app.get("/v1/reports/orders", dependencies=auth, tags=["Reports"])
    def report(request: Request):
        with runtime.scope(request) as profile:
            return profile.database.report()

    ui.mount(app)
    return app
