"""Resource control for HVD — budget tracking and throttling.

Tracks compute spend across all spawned entities. Can throttle, hibernate,
or kill entities that exceed their allocation.
"""

import time
from dataclasses import dataclass, field

from chimera.log import get_logger

log = get_logger("resource_control")


@dataclass
class GlobalBudget:
    """Global resource limits for HVD."""

    max_daily_cost_usd: float = 10.0
    max_concurrent_entities: int = 3
    max_single_entity_cost_usd: float = 3.0
    daily_cost_so_far: float = 0.0
    last_reset: float = field(default_factory=time.time)

    def track_cost(self, cost: float) -> None:
        """Add cost to the running total."""
        self._maybe_reset()
        self.daily_cost_so_far += cost
        log.info("cost tracked: +$%.2f (daily total: $%.2f/$%.2f)",
                 cost, self.daily_cost_so_far, self.max_daily_cost_usd)

    def budget_remaining(self) -> float:
        """How much budget is left today."""
        self._maybe_reset()
        return max(0.0, self.max_daily_cost_usd - self.daily_cost_so_far)

    def is_exhausted(self) -> bool:
        """Check if daily budget is used up."""
        self._maybe_reset()
        return self.daily_cost_so_far >= self.max_daily_cost_usd

    def _maybe_reset(self) -> None:
        """Reset daily counter if a new day has started."""
        now = time.time()
        if now - self.last_reset > 86400:  # 24 hours
            self.daily_cost_so_far = 0.0
            self.last_reset = now
            log.info("daily budget reset")

    def to_dict(self) -> dict:
        """Serialize for state storage."""
        return {
            "max_daily_cost_usd": self.max_daily_cost_usd,
            "max_concurrent_entities": self.max_concurrent_entities,
            "max_single_entity_cost_usd": self.max_single_entity_cost_usd,
            "daily_cost_so_far": self.daily_cost_so_far,
            "budget_remaining": self.budget_remaining(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalBudget":
        """Deserialize from state."""
        return cls(
            max_daily_cost_usd=d.get("max_daily_cost_usd", 10.0),
            max_concurrent_entities=d.get("max_concurrent_entities", 3),
            max_single_entity_cost_usd=d.get("max_single_entity_cost_usd", 3.0),
            daily_cost_so_far=d.get("daily_cost_so_far", 0.0),
        )
