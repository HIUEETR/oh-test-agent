from __future__ import annotations

from collections.abc import Callable

from ..models import EventType, RunEvent, RunTrace
from ..storage import ArtifactStore, RunRepository


class RunEventEmitter:
    def __init__(
        self,
        trace: RunTrace,
        repository: RunRepository,
        artifacts: ArtifactStore,
        callback: Callable[[RunEvent], None] | None = None,
    ):
        self.trace = trace
        self.repository = repository
        self.artifacts = artifacts
        self.callback = callback

    def emit(self, event_type: EventType, message: str, payload: dict | None = None) -> RunEvent:
        event = RunEvent(
            event_id=len(self.trace.events) + 1,
            run_id=self.trace.run_id,
            type=event_type,
            message=message,
            payload=payload or {},
        )
        self.trace.events.append(event)
        self.repository.add_event(event)
        self.repository.save_trace(self.trace)
        self.artifacts.save_trace(self.trace)
        if self.callback:
            self.callback(event)
        return event
