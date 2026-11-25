from contextlib import contextmanager
from dataclasses import dataclass
import threading

from filelock import FileLock, Timeout

from trading.broker import WebullGateway
from trading.database import Database
from trading.models import DomainError
from trading.service import TradingService
from trading.strategies import SumStrategyEngine


@dataclass
class Profile:
    config: object
    database: Database
    broker: object
    service: TradingService
    strategies: SumStrategyEngine


class ProfileManager:
    """One active UI context, separate ledgers/clients, serialized environment binding."""

    def __init__(self, config, brokers=None):
        self.lock = threading.RLock()
        self.active_environment = config.broker.environment
        self.profiles = {}
        self.process_locks = []
        configurations = config.profiles or {self.active_environment: config}
        supplied = brokers if isinstance(brokers, dict) else {self.active_environment: brokers}
        for environment, child in configurations.items():
            database = Database(child.database, child.identity)
            broker = supplied.get(environment) or WebullGateway(child.broker)
            service = TradingService(child, database, broker)
            self.profiles[environment] = Profile(child, database, broker, service, SumStrategyEngine(service))

    @property
    def active(self):
        return self.profiles[self.active_environment]

    def start(self):
        try:
            for environment in sorted(self.profiles):
                profile = self.profiles[environment]
                profile.config.database.parent.mkdir(parents=True, exist_ok=True)
                lock = FileLock(str(profile.config.database) + ".server.lock")
                lock.acquire(timeout=0)
                self.process_locks.append(lock)
            for profile in self.profiles.values():
                profile.database.initialize()
        except Exception as exc:
            for lock in reversed(self.process_locks):
                lock.release()
            self.process_locks.clear()
            if isinstance(exc, Timeout):
                raise RuntimeError("Another backend owns a profile database; run a single server worker.") from exc
            raise

    def stop(self):
        try:
            self.halt_all("Server shutdown: live arms invalidated")
        finally:
            for lock in reversed(self.process_locks):
                lock.release()
            self.process_locks.clear()

    @contextmanager
    def scope(self, request, mutation=False):
        # Enter/exit inside the same synchronous route thread, not a yield dependency.
        with self.lock:
            expected = request.headers.get("X-Trading-Environment")
            if expected and expected != self.active_environment:
                raise DomainError("environment_changed", "Active environment changed. Refresh and review the account again.")
            if mutation and self.active_environment == "production" and expected != "production":
                raise DomainError("environment_required", "Live actions require X-Trading-Environment: production.")
            yield self.active

    def summary(self):
        with self.lock:
            return [{
                "environment": environment, "label": "Live (real money)" if environment == "production" else "Sandbox",
                "configured": environment in self.profiles,
                "connected": bool(environment in self.profiles and self.profiles[environment].broker.connected),
                "selected": environment == self.active_environment,
            } for environment in ("sandbox", "production")]

    def halt_all(self, reason):
        with self.lock:
            # Revoke every in-memory authorization before touching any journal.
            for profile in self.profiles.values():
                profile.service._arm_deadline = 0
                profile.service._arm_nonce = ""
                profile.service._approval_deadlines.clear()
                profile.service._strategy_grants.clear()
            failures = []
            for profile in self.profiles.values():
                try:
                    profile.service.set_halt(True, reason)
                    for strategy in profile.strategies.active():
                        profile.strategies.pause(strategy["id"], reason)
                except Exception:
                    failures.append(profile.config.broker.environment)
            if failures:
                raise DomainError("halt_journal_failed", "All in-memory live/strategy authorizations were revoked, but a profile journal failed. Stop the server and review the database.", 503)
            return self.active.database.state("halt")

    def switch(self, request, expected):
        with self.lock:
            if expected != self.active_environment:
                raise DomainError("environment_changed", "Refresh the active environment before switching.")
            phrase = "SWITCH TO LIVE" if request.environment == "production" else "SWITCH TO SANDBOX"
            if request.confirmation != phrase:
                raise DomainError("environment_confirmation", f"Explicit acknowledgement {phrase} is required.")
            self.halt_all("Environment switch: all profiles halted")
            if request.environment not in self.profiles:
                raise DomainError("profile_not_configured", "Fill the selected profile's App Key, App Secret and Account ID in .env and restart.")
            current, target = self.active, self.profiles[request.environment]
            for profile in (current, target):
                if any(value > 0 for value in profile.database.reserved_shares().values()):
                    raise DomainError("strategy_exposure_blocks_switch", "Strategy-owned holdings require recovery/release before switching profiles.")
                if profile.database.orders(active=True):
                    raise DomainError("active_orders_block_switch", "Reconcile/cancel active or uncertain local orders before switching. Trading has been halted.")
                if profile.broker.connected and profile.broker.open_orders():
                    raise DomainError("broker_orders_block_switch", "Broker open orders exist. Review them before switching; trading is halted.")
            self.active_environment = request.environment
            target.database.audit_event("environment_selected", details={"environment": request.environment})
            return target

    def reconcile_connected(self):
        with self.lock:
            for environment, profile in self.profiles.items():
                if profile.broker.connected:
                    active = profile.strategies.active()
                    if active:
                        for strategy in active:
                            profile.strategies.tick(strategy["id"])
                    profile.service.reconcile_active(2 if environment == "production" else 5, exclude_strategy=bool(active))
