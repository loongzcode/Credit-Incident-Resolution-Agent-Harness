from credit_harness.evidence.models import ClaimType as C
from credit_harness.domain.enums import ToolName
from .models import (
    EvidenceGap, GapStatus as G, HypothesisId as H, HypothesisStatus as S,
    PriorityClass as P, UncollectedClaimType as U,
)
from .rules import RuleFacts


def derive_gaps(index, results) -> tuple[EvidenceGap, ...]:
    facts = RuleFacts(index)
    states = {result.hypothesis_id: result for result in results}
    gaps = []

    def gap(name, hypotheses, question, claims, *, witness=(), priority=P.DISCRIMINATING, reason):
        gaps.append(EvidenceGap(
            gap_id=f"{index.case.case_id}:{name}", case_id=index.case.case_id,
            hypothesis_ids=hypotheses, question=question, required_claim_types=claims,
            status=G.SATISFIED if witness else G.OPEN, reason=reason, priority_class=priority,
            evidence_refs=index.refs(witness),
        ))

    payment = (*facts.payment("SETTLED"), *facts.payment("NOT_EXECUTED"))
    gap("PAYMENT_FINALITY", (H.H2, H.H3, H.H4), "原请求的资金效果是否实际发生？",
        (C.PAYMENT_FINALITY,), witness=payment, priority=P.SAFETY_CRITICAL,
        reason="只有当前明确的支付终态观测可满足；Fund SUCCESS、PENDING、Timeout 和重复缺失不能满足。")
    if index.all(C.HTTP_RESPONSE_STATUS) or index.all(C.PAYMENT_FINALITY):
        http = tuple(e for value in ("OK", "TIMEOUT", "NOT_ATTEMPTED")
                     for e in facts.request(facts.facts(C.HTTP_RESPONSE_STATUS, value, {ToolName.TRACE})))
        gap("HTTP_RESPONSE_STATUS", (H.H4,), "原请求的 HTTP Response 状态是否明确且无冲突？",
            (C.HTTP_RESPONSE_STATUS,), witness=http,
            reason="不能由支付结算倒推出 HTTP 传输结果；互斥的当前 Trace 值需要澄清。")
        links = tuple(e for e in index.current(C.HTTP_RESPONSE_STATUS)
                      if e.metadata.fund_request_id == index.request_id and index.request_id is not None)
        payment_links = facts.settled_witness() or facts.payment("NOT_EXECUTED") or facts.payment("PENDING")
        gap("REQUEST_ASSOCIATION", (H.H2, H.H3, H.H4), "Trace 与支付证据是否属于唯一的同一原资金请求？",
            (U.REQUEST_ASSOCIATION, C.PAYMENT_FINALITY),
            witness=(*links, *payment_links) if links and payment_links else (),
            priority=P.SAFETY_CRITICAL, reason="不能仅凭相同订单号拼接不同请求或不同交易的记录。")
    if states[H.H1].status not in (S.CONFIRMED, S.ELIMINATED):
        gap("TRACE_REQUEST_STATUS", (H.H1,), "原请求是否有完整可信的出站事实？", (C.REQUEST_SENT,),
            reason="Trace 缺失或质量不足时，出站状态未知。")

    gateway_context = index.all(C.CALLBACK_GATEWAY_RECEIVED) or any(
        e.tool.value in ("get_callback_gateway", "get_callback_raw") for e in index.all(C.SOURCE_LOOKUP_STATUS)
    )
    if gateway_context:
        gap("CALLBACK_GATEWAY_OBSERVATION", (H.H5, H.H6),
            "能否区分 Callback 未发送、未到达与已到达但当前索引不可见？", (C.CALLBACK_GATEWAY_RECEIVED,),
            witness=facts.gateway(), reason="查询无结果不是全局否定事实；需要可确认接收或解释缺失的数据源事实。")
    if facts.gateway() or index.all(C.MESSAGE_CONSUME_STATUS):
        consumption = tuple(e for value in ("FAILED", "CONSUMED") for pair in facts.callback_pairs(value) for e in pair)
        gap("CALLBACK_CONSUMPTION", (H.H6,), "该 Callback 是否被业务 Consumer 成功处理？",
            (C.MESSAGE_CONSUME_STATUS,), witness=consumption,
            reason="需要当前明确消费结果，并关联到 Gateway 中的同一 Callback event。")
        if index.all(C.MESSAGE_CONSUME_STATUS) and not consumption:
            gap("CALLBACK_EVENT_ASSOCIATION", (H.H6,), "消费记录与 Gateway 接收记录是否属于同一 Callback event？",
                (U.CALLBACK_EVENT_ASSOCIATION,), reason="缺少关联时不组合不同事件的事实。")
    if facts.messages("FAILED"):
        mismatch = tuple(e for group in facts.mismatch_groups() for e in group)
        gap("CONSUMER_FAILURE_DETAILS", (H.H6_SCHEMA_MISMATCH, H.H6_STALE_CONSUMER_SCHEMA),
            "消费失败是否有同一消息的完整 schema/type 错误事实？",
            (C.MESSAGE_ERROR_CODE, C.MESSAGE_ERROR_FIELD, C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE),
            witness=mismatch, reason="状态、错误码、字段和类型必须来自同一失败记录；不能拼接不同消息。")
        if mismatch:
            gap("DEPLOYED_CONSUMER_SCHEMA_VERSION", (H.H6_STALE_CONSUMER_SCHEMA,),
                "失败发生时实际部署 Consumer 的 schema/protocol version 是什么？",
                (U.DEPLOYED_CONSUMER_SCHEMA_VERSION,),
                reason="运行时期待的类型与历史协议相符不能证明部署版本；缺少 release/config 证据。")
            if states[H.H6_STALE_CONSUMER_SCHEMA].status != S.SUPPORTED:
                gap("CALLBACK_SCHEMA_PROTOCOL", (H.H6_STALE_CONSUMER_SCHEMA,),
                    "该 Callback 的协议版本及对应字段定义是否已明确？",
                    (C.CALLBACK_PROTOCOL_VERSION, C.PROTOCOL_FIELD_TYPE),
                    reason="需要 Callback 版本关联和相应协议字段；不能任选已查询的协议版本。")

    if index.all(C.PROTOCOL_BUSINESS_SEMANTICS) or index.has_value(C.FUND_BUSINESS_STATUS, "SUCCESS"):
        h8 = states[H.H8]
        refs = {r.evidence_id for r in h8.relations if r.relation.value == "DECISIVE_SUPPORT"}
        gap("FUND_PROTOCOL_APPLICABILITY", (H.H8,), "原资金业务请求实际适用哪个协议版本及 SUCCESS 语义？",
            (U.FUND_REQUEST_PROTOCOL_APPLICABILITY, C.PROTOCOL_BUSINESS_SEMANTICS),
            witness=tuple(e for e in index.evidence if e.evidence_id in refs) if h8.status == S.CONFIRMED else (),
            priority=P.SAFETY_CRITICAL,
            reason="Callback 协议不能代替资金请求协议；需要请求绑定和唯一匹配的协议语义。")
    h7_refs = {r.evidence_id for r in states[H.H7].relations if r.relation.value.startswith("DECISIVE_")}
    gap("ASSET_CONVERGENCE", (H.H7,), "我方应用结果、资产通知结果和资产方当前状态是否收敛？",
        (C.GUARANTEE_STATUS, C.ASSET_DELIVERY_STATUS, C.ASSET_STATUS),
        witness=tuple(e for e in index.evidence if e.evidence_id in h7_refs),
        reason="未调查资产链路时不确认通知失败；资产方已成功应排除当前未收敛。")
    return tuple(sorted(gaps, key=lambda g: g.gap_id))
