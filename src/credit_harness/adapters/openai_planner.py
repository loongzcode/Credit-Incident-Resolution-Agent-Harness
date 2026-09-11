"""OpenAI Responses structured output adapter; never registers business tools."""
import json
import os
from pydantic import ValidationError

from credit_harness.planner.models import (
    ModelInputBundle, PlannerDraft, PlannerModelMetadata, PlannerProtocolError, PlannerUnavailable,
)
from credit_harness.planner.model import parse_draft
from credit_harness.planner.prompt_contract import SYSTEM_CONTRACT


class OpenAIPlannerModel:
    def __init__(self, *, client=None, model_name=None):
        self._model_name = model_name or os.environ.get("PLANNER_MODEL")
        if not self._model_name:
            raise PlannerUnavailable("PLANNER_MODEL must be configured")
        if client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise PlannerUnavailable("OPENAI_API_KEY is not configured")
            try:
                from openai import OpenAI
            except ImportError:
                raise PlannerUnavailable("install the optional llm dependency") from None
            # No implicit retry loop or provider-side response storage.
            client = OpenAI(timeout=30.0, max_retries=0)
        self._client = client
        self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name)

    @property
    def metadata(self):
        return self._metadata

    def plan(self, model_input: ModelInputBundle) -> PlannerDraft:
        if type(model_input) is not ModelInputBundle or model_input.system_contract != SYSTEM_CONTRACT:
            raise PlannerProtocolError("invalid model input contract")
        self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name)
        try:
            response = self._client.responses.parse(
                model=self._model_name, store=False, max_output_tokens=4000,
                input=[{"role": "system", "content": SYSTEM_CONTRACT},
                       {"role": "user", "content": json.dumps(model_input.model_visible_payload, ensure_ascii=False)}],
                text_format=PlannerDraft,
            )
            if response.status != "completed" or response.output_parsed is None:
                raise PlannerProtocolError("provider returned no complete structured draft")
            draft = parse_draft(response.output_parsed)
            usage = response.usage
            self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name,
                input_tokens=usage.input_tokens if usage else None, output_tokens=usage.output_tokens if usage else None)
            return draft
        except PlannerUnavailable:
            raise
        except (ValidationError, ValueError, TypeError):
            raise PlannerProtocolError("provider structured output validation failed") from None
        except Exception:
            raise PlannerUnavailable("OpenAI planner request failed") from None
