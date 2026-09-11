"""Full HTTP Harness -> HTTP Tool -> persisted Observation -> Evidence integration."""
from scripts.demo_case_evidence import run_demo
from credit_harness.cases.models import CaseStatus
from credit_harness.domain.enums import KnowledgeStatus, ScenarioId
from credit_harness.evidence.models import ClaimType as C


def test_s6_manual_investigation_through_harness_http(engine):
    view = run_demo(engine)
    assert view.case.case_id == "CASE-JD202609100001"
    assert view.case.status == CaseStatus.INVESTIGATING
    assert view.case.budget.used_tool_calls == 7 and view.evidence_count == 25
    expected = {
        C.HTTP_RESPONSE_STATUS: "TIMEOUT", C.FUND_BUSINESS_STATUS: "SUCCESS",
        C.PAYMENT_FINALITY: "SETTLED", C.PAYMENT_AMOUNT: 2_000_000, C.PAYMENT_CURRENCY: "CNY",
        C.CALLBACK_GATEWAY_RECEIVED: True, C.CALLBACK_SIGNATURE_VERIFIED: True,
        C.MESSAGE_CONSUME_STATUS: "FAILED", C.MESSAGE_DLQ: "loan.callback.dlq",
        C.MESSAGE_ERROR_CODE: "CALLBACK_SCHEMA_MISMATCH", C.MESSAGE_ERROR_FIELD: "loanNo",
    }
    for claim, value in expected.items():
        assert [item.value for item in view.evidence_by_claim_type[claim]] == [value]
    assert {(e.protocol_version, e.value) for e in view.evidence_by_claim_type[C.PROTOCOL_FIELD_TYPE]} == {
        ("2.3", "string"), ("2.2", "integer"),
    }
    assert not {"ROOT_CAUSE", "RETRY_LOAN", "PAYMENT_FAILED", "CALLBACK_NEVER_SENT", "GROUND_TRUTH"}.intersection(
        view.evidence_by_claim_type,
    )


def test_s8_repeated_http_failures_remain_unknown(engine):
    view = run_demo(engine, ScenarioId.S8)
    assert len(view.unknown_lookups) == 6
    assert view.payment_finality_evidence.knowledge == KnowledgeStatus.UNKNOWN
    assert C.PAYMENT_FINALITY not in view.evidence_by_claim_type
