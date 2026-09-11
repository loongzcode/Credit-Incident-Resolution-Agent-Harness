from typing import Protocol

from .models import AgentRunResult


class AgentRunTraceStore(Protocol):
    def append(self, result: AgentRunResult) -> None: ...


class InMemoryAgentRunTraceStore:
    """Append-only inspection traces, explicitly not a durable recovery checkpoint."""
    def __init__(self):
        self._records: list[AgentRunResult] = []

    def append(self, result: AgentRunResult) -> None:
        self._records.append(result)

    @property
    def records(self) -> tuple[AgentRunResult, ...]:
        return tuple(self._records)
