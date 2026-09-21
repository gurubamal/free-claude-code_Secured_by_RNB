"""Per-request provider failure limits, shared across automatic recovery rounds."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

PROVIDER_FAILURE_LIMIT = 3


@dataclass
class RouteAttemptBudget:
    failures: dict[str, int] = field(default_factory=dict)

    def allows(self, provider):
        return self.failures.get(provider, 0) < PROVIDER_FAILURE_LIMIT

    def record_failure(self, provider):
        self.failures[provider] = self.failures.get(provider, 0) + 1


_CURRENT: ContextVar[RouteAttemptBudget | None] = ContextVar(
    "fcc_route_budget", default=None
)


def current_route_budget():
    return _CURRENT.get()


@contextmanager
def use_route_budget(budget):
    token = _CURRENT.set(budget)
    try:
        yield
    finally:
        _CURRENT.reset(token)
