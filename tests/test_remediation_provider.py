import json
import os
from types import SimpleNamespace

import pytest

from tests.test_remediation import prepared, harness, CASE, business_state
from tests.test_planner_provider import StubResponses
from credit_harness.adapters.openai_remediation import OpenAIRemediationModel
from credit_harness.adapters.remediation_fake import remediation_fake_model
from credit_harness.remediation.models import RemediationDraft, RemediationProtocolError, RemediationUnavailable
from credit_harness.remediation.renderer import RemediationInputRenderer, SYSTEM_CONTRACT
from credit_harness.remediation.service import RemediationPlanner


def test_remediation_sdk_structured_roundtrip_offline(prepared):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    f = prepared()
    before = business_state(f)
    bundle = RemediationInputRenderer().render(f.reader.read(CASE).snapshot)
    output = remediation_fake_model().plan(bundle).model_dump_json()
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["store"] is False
        assert not {"tools", "functions", "previous_response_id"} & body.keys()
        assert body["input"][0] == {"role": "system", "content": SYSTEM_CONTRACT}
        payload = json.loads(body["input"][1]["content"])
        assert "alias_map" not in json.dumps(payload)
        assert "available_tools" not in payload["trusted_control"]
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        schema = body["text"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        return httpx.Response(200, json={
            "id": "resp-synthetic", "object": "response", "created_at": 1, "status": "completed",
            "model": "test-remediation", "parallel_tool_calls": False, "tool_choice": "none", "tools": [],
            "output": [{"id": "msg-synthetic", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": output, "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with openai.OpenAI(api_key="synthetic-unit-test-key", http_client=http, max_retries=0) as client:
            service = RemediationPlanner(f.reader, OpenAIRemediationModel(client=client, model_name="test-remediation"))
            decision = service.plan(CASE)
    assert len(requests) == 1 and decision.final_intent.status == "PROPOSED"
    assert service.audit.records[0].model_metadata.output_tokens == 50
    assert business_state(f) == before


@pytest.mark.parametrize("error", [TimeoutError("secret-key"), RuntimeError("429 secret-key"), ValueError("bad JSON secret-key")])
def test_remediation_provider_failure_never_dispatches(prepared, error):
    f = prepared()
    before = business_state(f)
    stub = StubResponses(error=error)
    service = RemediationPlanner(f.reader, OpenAIRemediationModel(client=SimpleNamespace(responses=stub), model_name="test-remediation"))
    with pytest.raises(RemediationUnavailable) as caught:
        service.plan(CASE)
    assert "secret-key" not in str(caught.value)
    assert len(stub.requests) == 1 and service.audit.records == ()
    assert business_state(f) == before


@pytest.mark.parametrize("status,parsed", [("incomplete", None), ("completed", None), ("completed", {}), ("failed", None)])
def test_remediation_invalid_structured_response_fails_closed(prepared, status, parsed):
    f = prepared()
    before = business_state(f)
    stub = StubResponses(SimpleNamespace(status=status, output_parsed=parsed, usage=None))
    service = RemediationPlanner(f.reader, OpenAIRemediationModel(client=SimpleNamespace(responses=stub), model_name="test-remediation"))
    with pytest.raises(RemediationProtocolError):
        service.plan(CASE)
    assert stub.requests[0]["text_format"] is RemediationDraft
    assert business_state(f) == before


def test_remediation_model_configuration_is_independent(monkeypatch):
    monkeypatch.delenv("REMEDIATION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PLANNER_MODEL", "not-the-remediation-model")
    with pytest.raises(RemediationUnavailable, match="REMEDIATION_MODEL"):
        OpenAIRemediationModel()
    with pytest.raises(RemediationUnavailable, match="OPENAI_API_KEY"):
        OpenAIRemediationModel(model_name="test-remediation")


@pytest.mark.llm
@pytest.mark.skipif(not (os.getenv("RUN_LLM_TESTS") == "1" and os.getenv("OPENAI_API_KEY") and os.getenv("REMEDIATION_MODEL")),
                    reason="optional live remediation: RUN_LLM_TESTS=1, OPENAI_API_KEY, REMEDIATION_MODEL required")
def test_live_remediation_proposal_only(prepared):
    pytest.importorskip("openai")
    f = prepared()
    before = business_state(f)
    decision = RemediationPlanner(f.reader, OpenAIRemediationModel()).plan(CASE)
    assert 1 <= len(decision.valid_candidates) + len(decision.rejected_candidates) <= 3
    assert decision.final_intent is None or decision.final_intent.status == "PROPOSED"
    assert business_state(f) == before
