from dataclasses import dataclass, field, replace
from decimal import Decimal
import hashlib
from pathlib import Path

from dotenv import dotenv_values

from webull_cli import ROOT, Settings


@dataclass(frozen=True)
class RiskLimits:
    max_order_quantity: int = 100
    max_order_notional: Decimal = Decimal("10000")
    max_symbol_exposure: Decimal = Decimal("20000")
    max_portfolio_exposure: Decimal = Decimal("100000")
    max_open_orders: int = 20
    max_daily_orders: int = 50
    max_daily_loss: Decimal = Decimal("1000")
    max_price_deviation: Decimal = Decimal("0.10")
    max_quote_age_seconds: int = 120
    fee_buffer: Decimal = Decimal("0.01")

    def __post_init__(self):
        for name, value in vars(self).items():
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError(f"{name} must be finite.")
            if value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.max_price_deviation > 1:
            raise ValueError("max_price_deviation must not exceed 1.")

    @classmethod
    def live_defaults(cls):
        return cls(max_order_quantity=5, max_order_notional=Decimal(500),
                   max_symbol_exposure=Decimal(1000), max_portfolio_exposure=Decimal(5000),
                   max_open_orders=3, max_daily_orders=5, max_daily_loss=Decimal(50),
                   max_price_deviation=Decimal("0.02"), max_quote_age_seconds=30)


def profile_settings(values, environment):
    """Production never falls back to legacy/sandbox credentials."""
    prefix = "WEBULL_SANDBOX_" if environment == "sandbox" else "WEBULL_LIVE_"
    fields = {name: values.get(prefix + name) or "" for name in ("APP_KEY", "APP_SECRET", "ACCOUNT_ID")}
    if environment == "sandbox" and not any(fields.values()):
        fields = {name: values.get("WEBULL_" + name) or "" for name in fields}
    return Settings.from_values({
        **{"WEBULL_" + name: value for name, value in fields.items()}, "WEBULL_ENV": environment,
        "WEBULL_ENABLE_LIVE_TRADING": (values.get("WEBULL_ENABLE_LIVE_TRADING") or "false") if environment == "production" else "false",
    })


@dataclass(frozen=True)
class AppConfig:
    broker: Settings
    api_key: str = field(repr=False)
    database: Path
    limits: RiskLimits = field(default_factory=RiskLimits)
    reconcile_interval: int = 15
    request_limit: int = 120
    live_arm_seconds: int = 600
    profiles: dict = field(default_factory=dict, repr=False)
    enable_live_strategies: bool = False
    strategy_max_pair_notional: Decimal = Decimal(1000)
    strategy_poll_seconds: int = 5

    def __post_init__(self):
        if len(self.api_key) < 32:
            raise ValueError("TRADING_API_KEY must contain at least 32 characters; run manage.py init.")
        if not self.broker.account_id:
            raise ValueError("WEBULL_ACCOUNT_ID is required.")
        if self.reconcile_interval < 0 or self.request_limit <= 0:
            raise ValueError("Invalid server timing/rate configuration.")
        if not 30 <= self.live_arm_seconds <= 1800:
            raise ValueError("Live arming duration must be between 30 and 1800 seconds.")
        if not self.strategy_max_pair_notional.is_finite() or self.strategy_max_pair_notional <= 0 or self.strategy_poll_seconds < 5:
            raise ValueError("Invalid strategy budget/poll settings.")

    @property
    def identity(self):
        return self.broker.environment + ":" + hashlib.sha256(self.broker.account_id.encode()).hexdigest()

    @classmethod
    def from_env(cls):
        # Personal backend: .env is the source of truth, not inherited shell values.
        values = dotenv_values(ROOT / ".env", interpolate=False)
        active = (values.get("WEBULL_ENV") or "sandbox").strip().lower()
        if active not in {"sandbox", "production"}:
            raise ValueError("WEBULL_ENV must be sandbox or production.")
        profiles = {}
        strategy_flag = (values.get("TRADING_ENABLE_LIVE_STRATEGIES") or "false").strip().lower()
        if strategy_flag not in {"true", "false"}:
            raise ValueError("TRADING_ENABLE_LIVE_STRATEGIES must be true or false.")
        for environment in ("sandbox", "production"):
            broker = profile_settings(values, environment)
            if not all((broker.app_key, broker.app_secret, broker.account_id)):
                if environment == active:
                    raise ValueError(f"Complete the {environment} profile's App Key, App Secret and Account ID in .env.")
                continue
            defaults = RiskLimits.live_defaults() if environment == "production" else RiskLimits()
            prefix = "TRADING_LIVE_" if environment == "production" else "TRADING_"
            limits = RiskLimits(**{name: type(default)(values.get(prefix + name.upper()) or str(default))
                                   for name, default in vars(defaults).items()})
            fingerprint = hashlib.sha256(broker.account_id.encode()).hexdigest()[:16]
            profiles[environment] = cls(
                broker=broker, api_key=(values.get("TRADING_API_KEY") or "").strip(),
                database=ROOT / "data" / f"{environment}_{fingerprint}.sqlite3", limits=limits,
                reconcile_interval=int(values.get("TRADING_RECONCILE_INTERVAL") or "15"),
                live_arm_seconds=int(values.get("TRADING_LIVE_ARM_SECONDS") or "600"),
                enable_live_strategies=strategy_flag == "true",
                strategy_max_pair_notional=Decimal(values.get("TRADING_STRATEGY_MAX_PAIR_NOTIONAL") or "1000"),
                strategy_poll_seconds=int(values.get("TRADING_STRATEGY_POLL_SECONDS") or "5"),
            )
        if len(profiles) == 2:
            sandbox, live = profiles["sandbox"].broker, profiles["production"].broker
            if any(getattr(sandbox, name) == getattr(live, name) for name in ("app_key", "app_secret", "account_id")):
                raise ValueError("Sandbox and live App Keys, App Secrets and Account IDs must be distinct.")
        return replace(profiles[active], profiles=profiles)
