"""Cost circuit breaker. Reads AGENT_CORE_MAX_USD_PER_TASK from env."""

import os
import threading
from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    pass


@dataclass
class Budget:
    max_usd: float
    _spent: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def charge(self, usd: float) -> None:
        with self._lock:
            self._spent += usd
            if self._spent > self.max_usd:
                raise BudgetExceededError(
                    f"Budget exceeded: ${self._spent:.4f} > ${self.max_usd:.4f}"
                )

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_usd - self._spent)


def get_budget() -> Budget:
    max_usd = float(os.environ.get("AGENT_CORE_MAX_USD_PER_TASK", "5.0"))
    return Budget(max_usd=max_usd)
