from credit_harness.evidence.models import ClaimType as C
from .models import HypothesisDefinition, HypothesisId as H, HypothesisKind as K

HYPOTHESIS_RULESET_VERSION = "3"


def definition(h, kind, statement, description, claims, *, parent=None, deferred=False):
    return HypothesisDefinition(
        hypothesis_id=h, kind=kind, statement=statement, description=description,
        confirmation_rule_id=f"{h.value}.{'confirmation_deferred' if deferred else 'confirm'}.v3",
        elimination_rule_id=f"{h.value}.eliminate.v3", relevant_claim_types=claims,
        parent_hypothesis_id=parent,
    )


CATALOG = (
    definition(H.H1, K.CAUSAL, "REQUEST_NOT_SENT", "原资金请求没有从我方成功发出。",
               (C.REQUEST_SENT,)),
    definition(H.H2, K.CAUSAL, "FUND_NOT_ACCEPTED", "资金方没有受理原资金请求。",
               (C.FUND_BUSINESS_STATUS, C.PAYMENT_FINALITY, C.TRANSACTION_FUND_REQUEST_ID,
                C.SOURCE_LOOKUP_STATUS), deferred=True),
    definition(H.H3, K.CAUSAL, "FUND_PROCESSING_FAILED", "资金方已受理但最终处理失败，未产生放款效果。",
               (C.FUND_BUSINESS_STATUS, C.PAYMENT_FINALITY, C.TRANSACTION_FUND_REQUEST_ID), deferred=True),
    definition(H.H4, K.STATE, "PAYMENT_SETTLED_WITHOUT_SUCCESSFUL_HTTP_RESPONSE",
               "原请求未获得成功 HTTP Response，但对应支付效果已有独立支付证据。",
               (C.HTTP_RESPONSE_STATUS, C.PAYMENT_FINALITY, C.TRANSACTION_FUND_REQUEST_ID)),
    definition(H.H5, K.CAUSAL, "CALLBACK_NOT_OBSERVED_AT_GATEWAY", "当前尚未观察到 Callback 到达 Gateway。",
               (C.SOURCE_LOOKUP_STATUS, C.CALLBACK_GATEWAY_RECEIVED), deferred=True),
    definition(H.H6, K.CAUSAL, "CALLBACK_CONSUMPTION_FAILED", "Callback 已到 Gateway，但业务消费未成功。",
               (C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS)),
    definition(H.H6_SCHEMA_MISMATCH, K.CAUSAL, "CALLBACK_SCHEMA_MISMATCH_TRIGGERED_FAILURE",
               "Callback 消费失败由 loanNo schema/type mismatch 直接触发。",
               (C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS, C.MESSAGE_ERROR_CODE, C.MESSAGE_ERROR_FIELD,
                C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE), parent=H.H6),
    definition(H.H6_STALE_CONSUMER_SCHEMA, K.CAUSAL, "STALE_CONSUMER_SCHEMA",
               "运行中的 Consumer 使用过期 schema，未能解析当前协议 Callback。",
               (C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS, C.MESSAGE_ERROR_CODE, C.MESSAGE_ERROR_FIELD,
                C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE,
                C.CALLBACK_PROTOCOL_VERSION, C.PROTOCOL_FIELD_TYPE), parent=H.H6, deferred=True),
    definition(H.H7, K.CAUSAL, "ASSET_NOTIFICATION_FAILED", "我方业务已成功应用，但资产方终态通知未收敛。",
               (C.GUARANTEE_STATUS, C.ASSET_DELIVERY_STATUS, C.ASSET_STATUS)),
    definition(H.H8, K.SEMANTIC_GUARD, "FUND_SUCCESS_NOT_EQUAL_PAYMENT_FINALITY",
               "当前资金请求适用协议中的业务 SUCCESS 本身不足以证明支付终态。",
               (C.PROTOCOL_BUSINESS_SEMANTICS, C.GUARANTEE_STATUS)),
)
