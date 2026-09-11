from enum import StrEnum


class LoanStatus(StrEnum):
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class FundBusinessStatus(StrEnum):
    NOT_RECEIVED = "NOT_RECEIVED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class PaymentFinality(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_EXECUTED = "NOT_EXECUTED"
    PENDING = "PENDING"
    SETTLED = "SETTLED"


class AccountingStatus(StrEnum):
    NOT_CREATED = "NOT_CREATED"
    POSTED = "POSTED"


class CallbackStatus(StrEnum):
    NOT_RECEIVED = "NOT_RECEIVED"
    RECEIVED = "RECEIVED"
    PARSE_FAILED = "PARSE_FAILED"
    APPLIED = "APPLIED"


class ConsumeStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"


class DeliveryStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class TransportStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    OK = "OK"
    TIMEOUT = "TIMEOUT"


class Currency(StrEnum):
    CNY = "CNY"
    USD = "USD"


class FieldType(StrEnum):
    INTEGER = "integer"
    STRING = "string"


class BusinessMeaning(StrEnum):
    LOAN_CREATED_PAYMENT_SEPARATE = "LOAN_CREATED_PAYMENT_SEPARATE"
    REQUEST_PROCESSING = "REQUEST_PROCESSING"
    REJECTED_NO_DISBURSEMENT = "REJECTED_NO_DISBURSEMENT"


class ToolName(StrEnum):
    ASSET = "get_asset_order"
    GUARANTEE = "get_guarantee_order"
    FUND = "get_fund_order"
    PAYMENT = "get_payment_transaction"
    LOAN_NOTE = "get_loan_note"
    CALLBACK = "get_callback_gateway"
    CALLBACK_RAW = "get_callback_raw"
    MESSAGES = "get_messages"
    ACCOUNTING = "get_accounting"
    TRACE = "get_request_trace"
    ASSET_DELIVERY = "get_asset_delivery"
    PROTOCOL = "get_protocol"


class ObservationStatus(StrEnum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class Freshness(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class KnowledgeStatus(StrEnum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


class SourceKind(StrEnum):
    PRIMARY = "PRIMARY"
    REPLICA = "REPLICA"
    CACHE = "CACHE"
    INDEX = "INDEX"


class FaultKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    DATA_DELAY = "DATA_DELAY"
    REPLICA_LAG = "REPLICA_LAG"
    INDEX_MISSING = "INDEX_MISSING"
    OLD_CACHE = "OLD_CACHE"


class ScenarioId(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"
    S8 = "S8"


class RootCause(StrEnum):
    REQUEST_NOT_SENT = "REQUEST_NOT_SENT"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    FUND_REJECTED = "FUND_REJECTED"
    RESPONSE_LOST = "RESPONSE_LOST"
    CALLBACK_NOT_DELIVERED = "CALLBACK_NOT_DELIVERED"
    CALLBACK_SCHEMA_MISMATCH = "CALLBACK_SCHEMA_MISMATCH"
    ASSET_NOTIFICATION_FAILED = "ASSET_NOTIFICATION_FAILED"
    PAYMENT_PENDING_UNOBSERVABLE = "PAYMENT_PENDING_UNOBSERVABLE"
