from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class AppConfig:
    broker: Settings
    api_key: str = field(repr=False)
    database: Path
    limits: RiskLimits = field(default_factory=RiskLimits)
    reconcile_interval: int = 15
    request_limit: int = 120

    def __post_init__(self):
        if len(self.api_key) < 32:
            raise ValueError("TRADING_API_KEY must contain at least 32 characters; run manage.py init.")
        if not self.broker.account_id:
            raise ValueError("WEBULL_ACCOUNT_ID is required.")
        if self.reconcile_interval < 0 or self.request_limit <= 0:
            raise ValueError("Invalid server timing/rate configuration.")

    @property
    def identity(self):
        return self.broker.environment + ":" + hashlib.sha256(self.broker.account_id.encode()).hexdigest()

    @classmethod
    def from_env(cls):
        # Personal backend: .env is the source of truth, not inherited shell values.
        values = dotenv_values(ROOT / ".env", interpolate=False)
        broker = Settings.from_values(values)
        fingerprint = hashlib.sha256(broker.account_id.encode()).hexdigest()[:16]
        limits = RiskLimits(**{
            name: type(default)(values.get("TRADING_" + name.upper()) or str(default))
            for name, default in vars(RiskLimits()).items()
        })
        return cls(
            broker=broker,
            api_key=(values.get("TRADING_API_KEY") or "").strip(),
            database=ROOT / "data" / f"{broker.environment}_{fingerprint}.sqlite3",
            limits=limits,
            reconcile_interval=int(values.get("TRADING_RECONCILE_INTERVAL") or "15"),
        )
