"""Optional structured proposal provider; no business tools, execution or credentials in audit."""
import json
import os
from pydantic import ValidationError
from credit_harness.planner.models import PlannerModelMetadata
from credit_harness.remediation.models import (RemediationInputBundle, RemediationDraft,
    RemediationProtocolError, RemediationUnavailable)
from credit_harness.remediation.renderer import SYSTEM_CONTRACT
from credit_harness.remediation.model import parse_draft


class OpenAIRemediationModel:
    def __init__(self, *, client=None, model_name=None):
        self._model_name = model_name or os.environ.get("REMEDIATION_MODEL")
        if not self._model_name:
            raise RemediationUnavailable("REMEDIATION_MODEL must be configured")
        if client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise RemediationUnavailable("OPENAI_API_KEY is not configured")
            try:
                from openai import OpenAI
            except ImportError:
                raise RemediationUnavailable("install optional llm dependency") from None
            client = OpenAI(timeout=30.0, max_retries=0)
        self._client = client
        self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name)

    @property
    def metadata(self):
        return self._metadata

    def plan(self, model_input):
        if type(model_input) is not RemediationInputBundle or model_input.system_contract != SYSTEM_CONTRACT:
            raise RemediationProtocolError("invalid remediation model input")
        self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name)
        try:
            response = self._client.responses.parse(model=self._model_name, store=False, max_output_tokens=4000,
                input=[{"role": "system", "content": SYSTEM_CONTRACT},
                       {"role": "user", "content": json.dumps(model_input.model_visible_payload, ensure_ascii=False)}],
                text_format=RemediationDraft)
            if response.status != "completed" or response.output_parsed is None:
                raise RemediationProtocolError("no complete structured remediation draft")
            draft = parse_draft(response.output_parsed)
            usage = response.usage
            self._metadata = PlannerModelMetadata(model_provider="openai", model_name=self._model_name,
                input_tokens=usage.input_tokens if usage else None, output_tokens=usage.output_tokens if usage else None)
            return draft
        except RemediationUnavailable:
            raise
        except (ValidationError, ValueError, TypeError):
            raise RemediationProtocolError("invalid structured remediation output") from None
        except Exception:
            raise RemediationUnavailable("remediation provider unavailable") from None
