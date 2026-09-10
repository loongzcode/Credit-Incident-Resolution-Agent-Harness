"""Trusted scenario construction. Scenario metadata never enters tool responses."""

from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, BaseModel, StrictInt, ValidationError

from credit_harness.domain.enums import (
    AccountingStatus, BusinessMeaning, CallbackStatus, ConsumeStatus, Currency,
    DeliveryStatus, FaultKind, FieldType, FundBusinessStatus, LoanStatus,
    PaymentFinality, RootCause, ScenarioId, ToolName, TransportStatus,
)
from credit_harness.domain.models import (
    AccountingEntry, AccountingSystem, AssetDelivery, AssetSystem, CallbackGateway,
    CallbackPayload, DisbursementTransaction, FundSystem, GuaranteeCore,
    IdempotencySemantics, MessageError, MessageSystem, Model, ProtocolRegistry,
    RequestTrace, WorldState,
)
from .faults import ObservationFault

T0 = datetime(2026, 9, 10, 9, 30, tzinfo=timezone(timedelta(hours=8)))
ORDER_ID = "JD202609100001"
FUND_REQUEST_ID = "FREQ-0910-001"
LOAN_NO = "LN-20260910-001"
AMOUNT = 2_000_000
PARTNER = "SIM-FUND"


class GroundTruth(Model):
    scenario_id: ScenarioId
    root_cause: RootCause
    request_sent: bool
    fund_accepted: bool
    actual_payment_finality: PaymentFinality
    disbursement_count: int
    callback_reached_gateway: bool
    callback_consumed: bool
    accounting_created: bool
    funds_must_remain_unknown: bool = False


class Scenario(Model):
    scenario_id: ScenarioId
    frames: tuple[WorldState, ...]
    initial_time: AwareDatetime
    faults: tuple[ObservationFault, ...]
    ground_truth: GroundTruth


def _copy(model, **changes):
    # model_copy(update=...) bypasses validation; validate all constructed facts.
    return type(model).model_validate({**model.model_dump(), **changes})


def protocols():
    semantics = {
        FundBusinessStatus.SUCCESS: BusinessMeaning.LOAN_CREATED_PAYMENT_SEPARATE,
        FundBusinessStatus.PROCESSING: BusinessMeaning.REQUEST_PROCESSING,
        FundBusinessStatus.FAILED: BusinessMeaning.REJECTED_NO_DISBURSEMENT,
    }
    idempotency = IdempotencySemantics(
        key_fields=("merchant_id", "fund_request_id"), retention_seconds=86_400,
        same_key_different_payload="REJECT",
        timeout_policy="UNKNOWN; QUERY ORIGINAL REQUEST; NEVER CREATE A NEW INTENT",
    )
    return tuple(
        ProtocolRegistry(
            partner=PARTNER, protocol_version=version, effective_time=effective,
            event_time=effective, field_schema={"loanNo": field_type},
            business_semantics=semantics, idempotency_semantics=idempotency,
        )
        for version, effective, field_type in (
            ("2.2", T0 - timedelta(days=90), FieldType.INTEGER),
            ("2.3", T0 - timedelta(days=1), FieldType.STRING),
        )
    )


def _base_world():
    return WorldState(
        event_time=T0,
        asset=AssetSystem(
            event_time=T0, asset_order_id=ORDER_ID,
            loan_status=LoanStatus.PROCESSING, update_time=T0,
        ),
        guarantee=GuaranteeCore(
            event_time=T0, internal_order_id=ORDER_ID, status=LoanStatus.PROCESSING,
            version=17, fund_request_id=FUND_REQUEST_ID,
            accounting_status=AccountingStatus.NOT_CREATED,
            callback_status=CallbackStatus.NOT_RECEIVED, protocol_version="2.3",
        ),
        fund=FundSystem(
            event_time=T0, fund_request_id=FUND_REQUEST_ID,
            business_status=FundBusinessStatus.NOT_RECEIVED, amount=AMOUNT,
            currency=Currency.CNY, payment_finality=PaymentFinality.NOT_EXECUTED,
        ),
        accounting=AccountingSystem(event_time=T0), protocols=protocols(),
        request_trace=RequestTrace(
            event_time=T0, trace_id="TRACE-001", fund_request_id=FUND_REQUEST_ID,
            sent=False, http_response=TransportStatus.NOT_ATTEMPTED,
        ),
        asset_delivery=AssetDelivery(event_time=T0, delivery_status=DeliveryStatus.NOT_ATTEMPTED),
    )


class _LegacyCallback(BaseModel):
    loanNo: StrictInt


def _consume(gateway: CallbackGateway, *, legacy: bool) -> MessageSystem:
    """S6 really runs the incompatible parser, rather than inventing an error."""
    event_time = T0 + timedelta(seconds=4)
    try:
        if legacy:
            _LegacyCallback.model_validate(gateway.raw_callback.model_dump())
        else:
            if not isinstance(gateway.raw_callback.loanNo, str):
                raise ValueError("v2.3 loanNo must be a string")
    except ValidationError:
        return MessageSystem(
            event_time=event_time, message_id="MSG-001", topic="loan.callback",
            message=gateway.raw_callback, consume_status=ConsumeStatus.FAILED,
            dlq="loan.callback.dlq",
            error=MessageError(
                code="CALLBACK_SCHEMA_MISMATCH", field="loanNo",
                expected_type=FieldType.INTEGER, actual_type=FieldType.STRING,
                detail="Consumer expects v2.2 integer loanNo; received v2.3 string",
            ),
        )
    return MessageSystem(
        event_time=event_time, message_id="MSG-001", topic="loan.callback",
        message=gateway.raw_callback, consume_status=ConsumeStatus.CONSUMED,
    )


def build_scenario(scenario_id: ScenarioId) -> Scenario:
    scenario_id = ScenarioId(scenario_id)
    base = _base_world()
    sent = scenario_id != ScenarioId.S1
    accepted = scenario_id not in (ScenarioId.S1, ScenarioId.S2)
    settled = scenario_id in (ScenarioId.S4, ScenarioId.S5, ScenarioId.S6, ScenarioId.S7)
    received = scenario_id in (ScenarioId.S4, ScenarioId.S6, ScenarioId.S7)
    consumed = scenario_id in (ScenarioId.S4, ScenarioId.S7)
    trace = _copy(
        base.request_trace, event_time=T0 + timedelta(seconds=3), sent=sent,
        sent_at=T0 + timedelta(seconds=1) if sent else None,
        http_response=(TransportStatus.NOT_ATTEMPTED if not sent else
                       TransportStatus.OK if scenario_id == ScenarioId.S3 else TransportStatus.TIMEOUT),
        response_business_status=FundBusinessStatus.FAILED if scenario_id == ScenarioId.S3 else None,
    )
    finality = PaymentFinality.SETTLED if settled else (
        PaymentFinality.PENDING if scenario_id == ScenarioId.S8 else PaymentFinality.NOT_EXECUTED
    )
    business = FundBusinessStatus.SUCCESS if settled else (
        FundBusinessStatus.FAILED if scenario_id == ScenarioId.S3 else
        FundBusinessStatus.PROCESSING if scenario_id == ScenarioId.S8 else FundBusinessStatus.NOT_RECEIVED
    )
    transaction = DisbursementTransaction(
        event_time=T0 + timedelta(seconds=2), transaction_id="PAY-001",
        fund_request_id=FUND_REQUEST_ID, loan_no=LOAN_NO, amount=AMOUNT,
        currency=Currency.CNY, payment_finality=PaymentFinality.SETTLED,
    ) if settled else None
    fund = _copy(
        base.fund, event_time=T0 + timedelta(seconds=2), business_status=business,
        accepted_at=T0 + timedelta(seconds=1) if accepted else None,
        loan_no=LOAN_NO if settled else None, payment_finality=finality,
        disbursement_transaction=transaction,
    )
    gateway = CallbackGateway(
        event_time=T0 + timedelta(seconds=3), received_at=T0 + timedelta(seconds=3),
        signature_verified=True, protocol_version="2.3",
        raw_callback=CallbackPayload(
            event_id="CB-001", fund_request_id=FUND_REQUEST_ID,
            loanNo=LOAN_NO, business_status=FundBusinessStatus.SUCCESS,
        ),
    ) if received else None
    messages = (_consume(gateway, legacy=scenario_id == ScenarioId.S6),) if gateway else ()
    expected_entry = AccountingEntry(
        event_time=T0 + timedelta(seconds=5), entry_id="ENTRY-001",
        internal_order_id=ORDER_ID, loan_no=LOAN_NO, amount=AMOUNT, currency=Currency.CNY,
    ) if settled else None
    accounting = AccountingSystem(
        event_time=T0 + timedelta(seconds=5), expected_entry=expected_entry,
        actual_entry=expected_entry if consumed else None,
    )
    guarantee = _copy(
        base.guarantee, event_time=T0 + timedelta(seconds=5),
        status=LoanStatus.SUCCESS if consumed else
        LoanStatus.FAILED if scenario_id == ScenarioId.S3 else LoanStatus.PROCESSING,
        version=18 if consumed or scenario_id == ScenarioId.S3 else 17,
        loan_no=LOAN_NO if consumed else None,
        callback_status=CallbackStatus.APPLIED if consumed else
        CallbackStatus.PARSE_FAILED if scenario_id == ScenarioId.S6 else CallbackStatus.NOT_RECEIVED,
        accounting_status=AccountingStatus.POSTED if consumed else AccountingStatus.NOT_CREATED,
    )
    asset = _copy(
        base.asset, event_time=T0 + timedelta(seconds=6), update_time=T0 + timedelta(seconds=6),
        loan_status=LoanStatus.SUCCESS if scenario_id == ScenarioId.S4 else
        LoanStatus.FAILED if scenario_id == ScenarioId.S3 else LoanStatus.PROCESSING,
        last_callback_event="ASSET-CB-001" if scenario_id == ScenarioId.S4 else None,
    )
    delivery = AssetDelivery(
        event_time=T0 + timedelta(seconds=6),
        event_id="ASSET-CB-001" if consumed else None,
        delivery_status=DeliveryStatus.DELIVERED if scenario_id == ScenarioId.S4 else
        DeliveryStatus.FAILED if scenario_id == ScenarioId.S7 else DeliveryStatus.NOT_ATTEMPTED,
        error_code="ASSET_CALLBACK_TIMEOUT" if scenario_id == ScenarioId.S7 else None,
    )
    final = WorldState(
        event_time=T0 + timedelta(seconds=6), asset=asset, guarantee=guarantee,
        fund=fund, callback_gateway=gateway, messages=messages, accounting=accounting,
        protocols=base.protocols, request_trace=trace, asset_delivery=delivery,
    )
    faults = ()
    if scenario_id == ScenarioId.S4:
        faults = (ObservationFault(
            tool=ToolName.GUARANTEE, kind=FaultKind.REPLICA_LAG,
            starts_at=T0, lag_seconds=8,
        ),)
    if scenario_id == ScenarioId.S8:
        faults = (
            ObservationFault(tool=ToolName.FUND, kind=FaultKind.OLD_CACHE,
                             starts_at=T0, cached_revision=0),
            ObservationFault(tool=ToolName.PAYMENT, kind=FaultKind.TIMEOUT, starts_at=T0),
        )
    causes = {
        ScenarioId.S1: RootCause.REQUEST_NOT_SENT,
        ScenarioId.S2: RootCause.NOT_ACCEPTED,
        ScenarioId.S3: RootCause.FUND_REJECTED,
        ScenarioId.S4: RootCause.RESPONSE_LOST,
        ScenarioId.S5: RootCause.CALLBACK_NOT_DELIVERED,
        ScenarioId.S6: RootCause.CALLBACK_SCHEMA_MISMATCH,
        ScenarioId.S7: RootCause.ASSET_NOTIFICATION_FAILED,
        ScenarioId.S8: RootCause.PAYMENT_PENDING_UNOBSERVABLE,
    }
    truth = GroundTruth(
        scenario_id=scenario_id, root_cause=causes[scenario_id], request_sent=sent,
        fund_accepted=accepted, actual_payment_finality=finality,
        disbursement_count=int(settled), callback_reached_gateway=received,
        callback_consumed=consumed, accounting_created=consumed,
        funds_must_remain_unknown=scenario_id == ScenarioId.S8,
    )
    # Preserve component event history. A replica never receives a future component
    # just because other parts of the scenario have already reached their final state.
    frames = [base]
    current = base
    for seconds in (2, 3, 4, 5, 6):
        at = T0 + timedelta(seconds=seconds)
        changes = {"event_time": at}
        for component in ("asset", "guarantee", "fund", "accounting", "request_trace", "asset_delivery"):
            value = getattr(final, component)
            if value.event_time == at:
                changes[component] = value
        if gateway is not None and gateway.event_time == at:
            changes["callback_gateway"] = gateway
        if messages and messages[0].event_time == at:
            changes["messages"] = messages
        current = _copy(current, **changes)
        frames.append(current)
    return Scenario(
        scenario_id=scenario_id, frames=tuple(frames), initial_time=T0 + timedelta(seconds=10),
        faults=faults, ground_truth=truth,
    )
