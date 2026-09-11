from dataclasses import dataclass

from credit_harness.cases.models import CaseStatus
from credit_harness.domain.enums import SourceKind, ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.identity.payment import verify_payment_identity
from credit_harness.identity.models import IdentityMatch
from .catalog import CATALOG
from .index import EvidenceIndex
from .models import EvidenceRelation, HypothesisId as H, HypothesisStatus as S, RelationKind as R


@dataclass(frozen=True)
class RuleResult:
    hypothesis_id: H
    status: S
    reason: str
    relations: tuple[EvidenceRelation, ...]


class RuleFacts:
    """Only indexed Evidence. Correlated witnesses remain full immutable records."""
    def __init__(self, index: EvidenceIndex):
        self.index = index

    def facts(self, claim, value, tools):
        return tuple(e for e in self.index.agreed(claim, value)
                     if e.tool in tools and e.source_kind == SourceKind.PRIMARY)

    def request(self, items):
        return self.index.for_request(items)

    def payment(self, value):
        return self.request(self.facts(C.PAYMENT_FINALITY, value, {T.PAYMENT}))

    def settled_witness(self):
        result = verify_payment_identity(self.index)
        return tuple(e for e in self.index.evidence if e.evidence_id in result.evidence_refs
                     ) if result.result == IdentityMatch.MATCH else ()

    def gateway(self):
        return self.facts(C.CALLBACK_GATEWAY_RECEIVED, True, {T.CALLBACK, T.CALLBACK_RAW})

    def messages(self, value):
        return self.facts(C.MESSAGE_CONSUME_STATUS, value, {T.MESSAGES})

    def callback_pairs(self, value):
        pairs = []
        for gateway in self.gateway():
            for message in self.messages(value):
                if (message.metadata.callback_event_id == gateway.subject.identifier
                        and gateway.event_time is not None and message.event_time is not None
                        and gateway.event_time <= message.event_time):
                    if value == "FAILED" and any(
                        m.metadata.callback_event_id == gateway.subject.identifier
                        and m.event_time >= message.event_time for m in self.messages("CONSUMED")
                        if m.event_time is not None
                    ):
                        continue
                    pairs.append((gateway, message))
        return tuple(pairs)

    def mismatch_groups(self):
        groups = []
        for failed in self.messages("FAILED"):
            values = {}
            for claim in (C.MESSAGE_ERROR_CODE, C.MESSAGE_ERROR_FIELD,
                          C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE):
                values[claim] = tuple(e for e in self.index.current(claim)
                                      if e.tool == T.MESSAGES and e.subject == failed.subject
                                      and e.observation_id == failed.observation_id
                                      and e.event_time == failed.event_time
                                      and e.metadata.callback_event_id == failed.metadata.callback_event_id
                                      and e.content_hash == failed.content_hash
                                      and e.source_kind == SourceKind.PRIMARY)
            if not all(values.values()):
                continue
            if (set(e.value for e in values[C.MESSAGE_ERROR_CODE]) != {"CALLBACK_SCHEMA_MISMATCH"}
                    or set(e.value for e in values[C.MESSAGE_ERROR_FIELD]) != {"loanNo"}):
                continue
            expected = {e.value for e in values[C.MESSAGE_EXPECTED_FIELD_TYPE]}
            actual = {e.value for e in values[C.MESSAGE_ACTUAL_FIELD_TYPE]}
            if len(expected) == len(actual) == 1 and expected != actual:
                groups.append((failed, *(e for group in values.values() for e in group)))
        return tuple(groups)

    def protocol_bindings(self):
        return self.request(tuple(e for e in self.index.current(C.GUARANTEE_STATUS)
                                  if e.tool == T.GUARANTEE and e.protocol_version
                                  and e.metadata.fund_request_id and e.source_kind == SourceKind.PRIMARY))


def evaluate_rules(index: EvidenceIndex) -> tuple[RuleResult, ...]:
    facts = RuleFacts(index)
    catalog = {d.hypothesis_id: d for d in CATALOG}
    results = []

    def decide(h, *, support=(), confirm=(), eliminate=(), contradict=(), reason):
        definition = catalog[h]
        default = S.POSSIBLE if index.case.status == CaseStatus.INVESTIGATING else S.UNKNOWN
        status = S.CONFIRMED if confirm else S.ELIMINATED if eliminate else S.SUPPORTED if support else default
        if confirm and eliminate:
            status, support, contradict = S.SUPPORTED, (*support, *confirm), (*contradict, *eliminate)
            confirm, eliminate = (), ()
            reason = "决定性候选证据互斥，不能确认或消除；需补充证据。"
        relations = {}
        for claim in definition.relevant_claim_types:
            for e in index.all(claim):
                relations[e.evidence_id] = EvidenceRelation(
                    case_id=index.case.case_id, hypothesis_id=h, evidence_id=e.evidence_id,
                    relation=R.CONTEXT_ONLY, reason="相关历史、查询结果或尚不满足关联/质量条件的上下文。",
                    rule_id=f"{h.value}.context.v3",
                )
        for items, relation, rule in (
            (support, R.SUPPORTS, f"{h.value}.support.v3"),
            (contradict, R.CONTRADICTS, f"{h.value}.contradict.v3"),
            (confirm, R.DECISIVE_SUPPORT, definition.confirmation_rule_id),
            (eliminate, R.DECISIVE_CONTRADICTION, definition.elimination_rule_id),
        ):
            for e in items:
                relations[e.evidence_id] = EvidenceRelation(
                    case_id=index.case.case_id, hypothesis_id=h, evidence_id=e.evidence_id,
                    relation=relation, reason=reason, rule_id=rule,
                )
        results.append(RuleResult(h, status, reason, tuple(relations[k] for k in sorted(relations))))

    # H1 is a single-field claim from the case-scoped Trace; it does not join
    # systems, so legacy Trace evidence needs no newly added identity metadata.
    sent = facts.facts(C.REQUEST_SENT, True, {T.TRACE})
    not_sent = facts.facts(C.REQUEST_SENT, False, {T.TRACE})
    decide(H.H1, confirm=not_sent, eliminate=sent,
           reason="完整当前 Trace 的出站标记决定该命题；缺失或不完整 Trace 不具有否定证明能力。")

    settled = facts.settled_witness()
    fund_records = tuple(e for value in ("PROCESSING", "SUCCESS", "FAILED")
                         for e in facts.request(facts.facts(C.FUND_BUSINESS_STATUS, value, {T.FUND})))
    decide(H.H2, eliminate=(*settled, *fund_records),
           reason="同请求可靠资金业务记录或完整 Payment Identity MATCH 可排除未受理；NOT_FOUND 不能确认未受理。")
    failed_fund = facts.request(facts.facts(C.FUND_BUSINESS_STATUS, "FAILED", {T.FUND}))
    decide(H.H3, support=failed_fund, eliminate=settled,
           reason="业务 FAILED 仅支持；完整 Payment Identity MATCH 否定未产生放款效果。本版缺少受理/拒绝的完整终态契约。")

    timeout = facts.request(facts.facts(C.HTTP_RESPONSE_STATUS, "TIMEOUT", {T.TRACE}))
    ok = facts.request(facts.facts(C.HTTP_RESPONSE_STATUS, "OK", {T.TRACE}))
    # Explicit trace link is required even if a payment record identifies a request.
    timeout = tuple(e for e in timeout if e.metadata.fund_request_id == index.request_id)
    decide(H.H4, support=(*timeout, *facts.payment("SETTLED")),
           confirm=(*timeout, *settled) if timeout and settled else (), eliminate=ok,
           reason="原请求 HTTP TIMEOUT + Payment Identity MATCH：交易、请求、金额、币种、客户、收款主体和账户完整绑定；不定位网络原因。")

    absent = tuple(e for e in index.all(C.SOURCE_LOOKUP_STATUS)
                   if e.tool in (T.CALLBACK, T.CALLBACK_RAW) and e.value == "NOT_FOUND")
    decide(H.H5, support=absent, eliminate=facts.gateway(),
           reason="查询范围内未观察到 Callback 仅为支持；实际 Gateway 接收证据排除此命题，不推导从未发送。")

    failed_pairs = tuple(e for pair in facts.callback_pairs("FAILED") for e in pair)
    consumed_pairs = tuple(e for pair in facts.callback_pairs("CONSUMED") for e in pair)
    decide(H.H6, support=(*facts.gateway(), *facts.messages("FAILED")), confirm=failed_pairs,
           eliminate=consumed_pairs if not failed_pairs else (),
           reason="同一 Callback event 的 Gateway 接收与消费 FAILED 确认消费异常；消费完成可排除当前未成功。")

    mismatches = facts.mismatch_groups()
    schema_witness = tuple(e for group in mismatches for gateway, message in facts.callback_pairs("FAILED")
                           if group[0].evidence_id == message.evidence_id for e in (gateway, *group))
    decide(H.H6_SCHEMA_MISMATCH, support=tuple(e for group in mismatches for e in group) or facts.messages("FAILED"),
           confirm=schema_witness,
           reason="父级同一 Callback 的 Gateway/消费失败 witness，加上同消息、同观测、同事件的错误码、字段和互异类型。")

    stale_support = []
    fields = index.current(C.PROTOCOL_FIELD_TYPE)
    versions = index.current(C.CALLBACK_PROTOCOL_VERSION)
    for group in mismatches:
        message = group[0]
        parent_witness = tuple(e for pair in facts.callback_pairs("FAILED")
                               if pair[1].evidence_id == message.evidence_id for e in pair)
        if not parent_witness:
            continue
        expected = next(e.value for e in group if e.claim_type == C.MESSAGE_EXPECTED_FIELD_TYPE)
        actual = next(e.value for e in group if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE)
        for version in versions:
            if version.subject.identifier != message.metadata.callback_event_id:
                continue
            current_docs = [e for e in fields if e.subject.field == "loanNo"
                            and e.protocol_version == version.value and e.value == actual]
            for doc in current_docs:
                partner = doc.subject.identifier.rsplit("@", 1)[0]
                older_docs = [e for e in fields if e.subject.field == "loanNo" and e.value == expected
                              and e.subject.identifier.rsplit("@", 1)[0] == partner
                              and e.protocol_version != doc.protocol_version
                              and e.event_time is not None and doc.event_time is not None
                              and e.event_time < doc.event_time]
                if older_docs:
                    stale_support.extend((*parent_witness, *group, version, doc, *older_docs))
    decide(H.H6_STALE_CONSUMER_SCHEMA, support=stale_support,
           reason="类型与历史协议相符只构成支持；没有部署 schema/release/config 证据，禁止确认运行版本。")

    applied = facts.facts(C.GUARANTEE_STATUS, "SUCCESS", {T.GUARANTEE})
    delivery_failed = facts.facts(C.ASSET_DELIVERY_STATUS, "FAILED", {T.ASSET_DELIVERY})
    asset_pending = facts.facts(C.ASSET_STATUS, "PROCESSING", {T.ASSET})
    asset_success = facts.facts(C.ASSET_STATUS, "SUCCESS", {T.ASSET})
    decide(H.H7, support=(*applied, *delivery_failed),
           confirm=(*applied, *delivery_failed, *asset_pending) if applied and delivery_failed and asset_pending else (),
           eliminate=asset_success,
           reason="我方成功、通知失败及资产方仍处理中共同确认当前未收敛；资产方已成功则排除。")

    semantics = tuple(e for e in facts.facts(C.PROTOCOL_BUSINESS_SEMANTICS,
                                           "LOAN_CREATED_PAYMENT_SEPARATE", {T.PROTOCOL})
                      if e.subject.field == "SUCCESS")
    applicable = []
    bindings = facts.protocol_bindings()
    binding_versions = {e.protocol_version for e in bindings}
    if len(binding_versions) == 1:
        for binding in bindings:
            docs = [e for e in semantics if e.protocol_version == binding.protocol_version]
            # Check ALL current SUCCESS documents, not just documents agreeing
            # with our desired semantic guard; a second partner is ambiguous.
            candidates = [e for e in index.current(C.PROTOCOL_BUSINESS_SEMANTICS)
                          if e.tool == T.PROTOCOL and e.subject.field == "SUCCESS"
                          and e.protocol_version == binding.protocol_version]
            if docs and len({e.subject.identifier for e in candidates}) == 1:
                applicable.extend((binding, *docs))
    decide(H.H8, support=semantics, confirm=applicable,
           reason="业务 SUCCESS 的证明边界由适用协议确定；Callback 版本不等于原资金请求版本，支付已结算也不否定语义守卫。")
    return tuple(results)
