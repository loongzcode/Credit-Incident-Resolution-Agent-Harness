from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from .models import ToolCapability, ToolDataClass as D


def capability(tool, description, claims, classification=D.BUSINESS):
    return ToolCapability(tool_name=tool, description=description,
                          produces_claim_types=(*claims, C.SOURCE_LOOKUP_STATUS), data_classification=classification)


class ToolCapabilityCatalog:
    entries = (
        capability(T.ASSET, "查询资产方订单状态。", (C.ASSET_STATUS,)),
        capability(T.GUARANTEE, "查询担保核心状态与版本。", (C.GUARANTEE_STATUS, C.GUARANTEE_VERSION)),
        capability(T.FUND, "查询资金方业务状态与借据引用，不能替代支付终态。",
                   (C.FUND_BUSINESS_STATUS, C.LOAN_NO_PRESENT, C.LOAN_NOTE_REFERENCE)),
        capability(T.PAYMENT, "查询支付终态、交易与 tokenized 身份字段。",
                   (C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID,
                    C.PAYMENT_AMOUNT, C.PAYMENT_CURRENCY, C.PAYMENT_CUSTOMER_REF,
                    C.PAYMENT_BENEFICIARY_REF, C.PAYMENT_ACCOUNT_REF), D.TOKENIZED_FINANCIAL_IDENTITY),
        capability(T.LOAN_NOTE, "查询借据是否存在及其业务引用。", (C.LOAN_NO_PRESENT, C.LOAN_NOTE_REFERENCE)),
        capability(T.CALLBACK, "查询 Gateway 接收、验签与回调版本。",
                   (C.CALLBACK_GATEWAY_RECEIVED, C.CALLBACK_SIGNATURE_VERIFIED, C.CALLBACK_PROTOCOL_VERSION)),
        capability(T.CALLBACK_RAW, "查询受限回调业务 DTO；Context 仅使用提取后的结构化字段。",
                   (C.CALLBACK_GATEWAY_RECEIVED, C.CALLBACK_SIGNATURE_VERIFIED, C.CALLBACK_PROTOCOL_VERSION)),
        capability(T.MESSAGES, "查询消息消费、DLQ 与结构化 schema 错误字段。",
                   (C.MESSAGE_CONSUME_STATUS, C.MESSAGE_DLQ, C.MESSAGE_ERROR_CODE, C.MESSAGE_ERROR_FIELD,
                    C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE)),
        capability(T.ACCOUNTING, "查询实际账务记录是否存在。", (C.ACCOUNTING_ENTRY_PRESENT,)),
        capability(T.TRACE, "查询原请求出站标记与 HTTP 状态。", (C.REQUEST_SENT, C.HTTP_RESPONSE_STATUS)),
        capability(T.ASSET_DELIVERY, "查询资产通知投递状态。", (C.ASSET_DELIVERY_STATUS,)),
        capability(T.PROTOCOL, "查询指定版本的结构化字段类型和业务语义。",
                   (C.PROTOCOL_FIELD_TYPE, C.PROTOCOL_BUSINESS_SEMANTICS), D.BUSINESS_RULE),
    )

    def for_case(self, case):
        return tuple(sorted((entry for entry in self.entries if entry.tool_name in case.scope.allowed_tools
                             and entry.tool_name.value not in case.constraints.forbidden_actions),
                            key=lambda e: e.tool_name.value))
