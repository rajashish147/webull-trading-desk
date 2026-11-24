from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,19}$")]
Price = Annotated[Decimal, Field(gt=0, le=1000000, max_digits=13, decimal_places=6, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderSpec(StrictModel):
    symbol: Symbol
    side: Literal["BUY", "SELL"]
    quantity: Annotated[int, Field(gt=0, le=100000, strict=True)]
    limit_price: Price
    order_type: Literal["LIMIT"] = "LIMIT"
    time_in_force: Literal["DAY", "GTC"] = "DAY"
    trading_session: Literal["CORE"] = "CORE"

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("limit_price")
    @classmethod
    def normalize_price(cls, value):
        return value.normalize()

    def payload(self, client_order_id):
        return {
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "limit_price": format(self.limit_price, "f"),
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "support_trading_session": self.trading_session,
            "entrust_type": "QTY",
            "instrument_type": "EQUITY",
            "market": "US",
        }


class ReplaceOrder(StrictModel):
    quantity: Annotated[int, Field(gt=0, le=100000, strict=True)]
    limit_price: Price


class HaltRequest(StrictModel):
    enabled: bool
    reason: Annotated[str, Field(min_length=3, max_length=200)]


class WatchlistRequest(StrictModel):
    symbols: Annotated[list[Symbol], Field(max_length=100)]


class EnvironmentRequest(StrictModel):
    environment: Literal["sandbox", "production"]
    confirmation: str


class LiveArmRequest(StrictModel):
    confirmation: Literal["ENABLE LIVE TRADING"]


class LiveApprovalRequest(StrictModel):
    operation: Literal["SUBMIT", "REPLACE", "CANCEL"]
    quantity: Annotated[int, Field(gt=0, le=100000, strict=True)] | None = None
    limit_price: Price | None = None

    @model_validator(mode="after")
    def validate_parameters(self):
        if self.operation == "REPLACE":
            if self.quantity is None or self.limit_price is None:
                raise ValueError("Replacement approval requires quantity and limit_price.")
        elif self.quantity is not None or self.limit_price is not None:
            raise ValueError("Submit/cancel approvals cannot override order parameters.")
        return self


class DomainError(Exception):
    def __init__(self, code, message, status=409, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class BrokerError(DomainError):
    def __init__(self, message, definitive=False, status=502):
        super().__init__("broker_error", message, status)
        self.definitive = definitive


TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
ACTIVE = {"SUBMITTING", "ACKNOWLEDGED", "OPEN", "PARTIALLY_FILLED", "MODIFYING", "CANCELLING", "UNKNOWN"}
