"""Open the runner journal read-write only for an explicit guardian reset."""
from __future__ import annotations

from pathlib import Path


class GuardianWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save_guardian_state(self, *, day: str, day_start_equity: float | None,
                            halted_day: str | None, halt_reason: str = "",
                            halted_at: float | None = None) -> None:
        from core.state_store import StateStore

        store = StateStore(str(self.path))
        try:
            store.save_guardian_state(
                day=day, day_start_equity=day_start_equity,
                halted_day=halted_day, halt_reason=halt_reason,
                halted_at=halted_at)
        finally:
            store.close()


__all__ = ["GuardianWriter"]
