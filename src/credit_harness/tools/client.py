"""HTTP-only client. Does not import storage, scenario seeds, or WorldState."""

import httpx
from pydantic import TypeAdapter

from credit_harness.domain.enums import ToolName
from .contracts import DISPATCH_CORRELATION_HEADER, DispatchCorrelationId, Observation, ToolQuery


class ToolClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def observe(self, tool: ToolName, query: ToolQuery, *,
                dispatch_correlation_id: DispatchCorrelationId | None = None) -> Observation:
        headers = {}
        if dispatch_correlation_id is not None:
            headers[DISPATCH_CORRELATION_HEADER] = TypeAdapter(DispatchCorrelationId).validate_python(
                dispatch_correlation_id,
            )
        # Per-request headers: a shared client never retains another dispatch's ID.
        response = self._http.post(f"/tools/{tool.value}", json=query.model_dump(mode="json"), headers=headers)
        response.raise_for_status()
        return Observation.model_validate(response.json())

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
