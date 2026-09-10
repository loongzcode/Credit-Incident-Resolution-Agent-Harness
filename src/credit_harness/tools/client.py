"""HTTP-only client. Does not import storage, scenario seeds, or WorldState."""

import httpx

from credit_harness.domain.enums import ToolName
from .contracts import Observation, ToolQuery


class ToolClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def observe(self, tool: ToolName, query: ToolQuery) -> Observation:
        response = self._http.post(f"/tools/{tool.value}", json=query.model_dump(mode="json"))
        response.raise_for_status()
        return Observation.model_validate(response.json())

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

