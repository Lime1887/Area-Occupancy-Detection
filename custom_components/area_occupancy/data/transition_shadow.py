"""Shadow-mode area transitions learned live, without the database (#603).

Adjacency learns which room people go to next from ``db.transitions``,
which replays each area's occupied intervals out of SQLite every hour.
The same detection can run as it happens: each area's live ground-truth
label (:mod:`.ground_truth`) going occupied is a "start" and going empty
an "end", and :class:`TransitionShadow` applies exactly the database's
rules to that stream: the same transition and trajectory windows, the same
adjacency check, 1-hop ``X -> Y`` and 2-hop ``W -> X -> Y`` chains bucketed
by hour of week, and the same recency half-life.

Shadow-mode contract: counted, persisted and compared daily with the
database's counts (diagnostics ``transition_shadow``); the adjacency boost
keeps reading the database snapshot until #603's switch-over.
"""

from __future__ import annotations



from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from typing import Any



from ..const import (
    ADJACENCY_RECENCY_HALF_LIFE_DAYS,
    ADJACENCY_TRAJECTORY_WINDOW_S,
    ADJACENCY_TRANSITION_WINDOW_S,
)
from ..time_utils import ensure_utc_datetime, to_local



# A conditional next-room share needs this much decayed count behind it
# before it is compared (a single walk-through is not a distribution).
TRANSITION_MIN_COUNT = 5.0



Counts = dict[str, dict[int, dict[str, float]]]  # "from|mid" -> hour -> to -> n


def _hour_of_week(ts: datetime) -> int:
    local = to_local(ts)
    return local.weekday() * 24 + local.hour



def _finite_nonnegative(value: Any, default: float = 0.0) -> float:
    """Return a finite, non-negative float, or the supplied default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default



@dataclass
class TransitionShadowState:
    """Live transition counts and the detector's short memory (persisted)."""



    counts: Counts = field(default_factory=dict)
    last_decay: datetime | None = None
    # Areas currently labelled occupied, and recent "end" events.
    occupied: dict[str, bool] = field(default_factory=dict)
    recent_ends: list[tuple[str, datetime]] = field(default_factory=list)
    # Daily worst gap to the database's next-room shares, oldest first.
    diff_history: list[dict[str, Any]] = field(default_factory=list)



    def to_dict(self) -> dict[str, Any]:
        """Serialize for the HA storage helper (JSON-safe)."""
        return {
            "counts": {
                chain: {str(h): dict(t) for h, t in by_hour.items()}
                for chain, by_hour in self.counts.items()
            },
            "last_decay": self.last_decay.isoformat() if self.last_decay else None,
            "occupied": dict(self.occupied),
            "recent_ends": [(a, t.isoformat()) for a, t in self.recent_ends],
            "diff_history": list(self.diff_history),
        }



    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransitionShadowState:
        """Restore from storage; malformed payloads fall back to empty."""
