import json
import os
from types import SimpleNamespace

import pytest

from credit_harness.adapters.openai_planner import OpenAIPlannerModel
from credit_harness.planner.models import PlannerDraft, PlannerProtocolError, PlannerUnavailable
from credit_harness.planner.renderer import ModelInputRenderer
from credit_harness.planner.service import PlannerService
from credit_harness.planner.prompt_contract import SYSTEM_CONTRACT
from tests.test_planner import inputs, snapshot, call, draft


class StubResponses:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def parse(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def test_openai_adapter_uses_structured_outputs_without_tools(snapshot):
    response = SimpleNamespace(status="completed", output_parsed=draft(snapshot, call(snapshot)),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))
    stub = StubResponses(response)
    model = OpenAIPlannerModel(client=SimpleNamespace(responses=stub), model_name="configured-test-model")
    decision = PlannerService(model).plan(snapshot)
    request = stub.requests[0]
    assert request["text_format"] is PlannerDraft
    assert request["store"] is False
    assert "tools" not in request and "functions" not in request and "previous_response_id" not in request
    assert request["input"][0] == {"role": "system", "content": SYSTEM_CONTRACT}
    payload = json.loads(request["input"][1]["content"])
    assert "alias_map" not in payload
    assert "untrusted_external_data" in payload and "deterministic_derived" in payload
    assert decision.planner_model_metadata.input_tokens == 100


@pytest.mark.parametrize("error", [TimeoutError("secret-key"), RuntimeError("429 secret-key"), ValueError("bad JSON secret-key")])
def test_provider_failures_are_safe(snapshot, error):
    stub = StubResponses(error=error)
    model = OpenAIPlannerModel(client=SimpleNamespace(responses=stub), model_name="configured-test-model")
    service = PlannerService(model)
    with pytest.raises(PlannerUnavailable) as caught:
        service.plan(snapshot)
    assert "secret-key" not in str(caught.value)
    assert len(stub.requests) == 1 and service.audit.records == ()


@pytest.mark.parametrize("status,parsed", [("incomplete", None), ("completed", None), ("completed", {}), ("failed", None)])
def test_provider_incomplete_refused_or_invalid_draft_fails_closed(snapshot, status, parsed):
    stub = StubResponses(SimpleNamespace(status=status, output_parsed=parsed, usage=None))
    model = OpenAIPlannerModel(client=SimpleNamespace(responses=stub), model_name="configured-test-model")
    with pytest.raises(PlannerProtocolError):
        PlannerService(model).plan(snapshot)


def test_openai_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("PLANNER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(PlannerUnavailable, match="PLANNER_MODEL"):
        OpenAIPlannerModel()
    with pytest.raises(PlannerUnavailable, match="OPENAI_API_KEY"):
        OpenAIPlannerModel(model_name="configured-test-model")


def test_sdk_structured_output_roundtrip_offline(snapshot):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx2")
    requests = []
    output = draft(snapshot, call(snapshot)).model_dump_json()
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert "tools" not in body and "functions" not in body
        schema = body["text"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        return httpx.Response(200, json={
            "id": "resp-synthetic", "object": "response", "created_at": 1, "status": "completed",
            "model": "configured-test-model", "parallel_tool_calls": False, "tool_choice": "none", "tools": [],
            "output": [{"id": "msg-synthetic", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": output, "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with openai.OpenAI(api_key="synthetic-unit-test-key", http_client=http, max_retries=0) as client:
            service = PlannerService(OpenAIPlannerModel(client=client, model_name="configured-test-model"))
            decision = service.plan(snapshot)
    assert len(requests) == 1
    assert decision.selected_action.candidate.tool_name == call(snapshot).tool_name
    assert service.audit.records[0].usage_metadata.output_tokens == 50


@pytest.mark.llm
@pytest.mark.skipif(not (os.getenv("RUN_LLM_TESTS") == "1" and os.getenv("OPENAI_API_KEY") and os.getenv("PLANNER_MODEL")),
                    reason="optional live LLM: requires RUN_LLM_TESTS=1, OPENAI_API_KEY and PLANNER_MODEL")
def test_live_llm_structured_proposal_only(snapshot):
    pytest.importorskip("openai")
    service = PlannerService(OpenAIPlannerModel())
    decision = service.plan(snapshot)
    assert decision.snapshot_id == snapshot.snapshot_id
    # A live model may propose bad candidates. Safe rejection is a valid outcome;
    # the integration contract is structured output + Harness control, not accuracy.
    assert 1 <= len(decision.valid_candidates) + len(decision.rejected_candidates) <= 3
    assert len(service.audit.records) == 1
