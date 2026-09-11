from credit_harness.cases.models import Case, CasePolicyError
from credit_harness.domain.enums import Freshness, ObservationStatus, SourceKind, ToolName
from credit_harness.tools.contracts import (
    AccountingData, AssetData, CallbackData, CallbackRawData, DeliveryData, FundData,
    GuaranteeData, LoanNoteData, MessagesData, Observation, PaymentData, ProtocolData,
    ToolQuery, TraceData,
)
from .models import (
    ClaimType as C, Evidence, EvidenceMetadata, EvidenceStrength,
    EvidenceSubject, SubjectKind as S, fingerprint, json_hash,
)

DATA_TYPES = {
    ToolName.ASSET: AssetData, ToolName.GUARANTEE: GuaranteeData,
    ToolName.FUND: FundData, ToolName.PAYMENT: PaymentData, ToolName.LOAN_NOTE: LoanNoteData,
    ToolName.CALLBACK: CallbackData, ToolName.CALLBACK_RAW: CallbackRawData,
    ToolName.MESSAGES: MessagesData, ToolName.ACCOUNTING: AccountingData,
    ToolName.TRACE: TraceData, ToolName.ASSET_DELIVERY: DeliveryData, ToolName.PROTOCOL: ProtocolData,
}


class EvidenceExtractor:
    def extract(self, case: Case, observation: Observation, *, query: ToolQuery | None = None) -> list[Evidence]:
        """Pure, deterministic extraction. Persistence verifies the supplied Observation.

        Optional query preserves exact requested protocol/effective-time scope; runtime
        always supplies the persisted request. No metadata is guessed from hidden state.
        """
        o = observation
        query = query or ToolQuery(internal_order_id=o.internal_order_id)
        if (o.internal_order_id not in case.scope.allowed_order_ids
                or o.tool not in case.scope.allowed_tools
                or query.internal_order_id != o.internal_order_id):
            raise CasePolicyError("observation outside case scope")
        data = o.data
        if data is not None and type(data) is not DATA_TYPES[o.tool]:
            raise ValueError("tool and observation data type mismatch")
        output = []
        # created_at is the extraction input's observation time, ensuring repeatability.
        # Case updated_at and conflict detected_at use the runtime clock separately.
        def emit(claim, value, path, *, kind=S.ORDER, identifier=None, field=None,
                 event_time=None, protocol=None, version=None):
            strength = (EvidenceStrength.HINT if claim == C.SOURCE_LOOKUP_STATUS else
                        EvidenceStrength.WEAK if o.freshness != Freshness.CURRENT else
                        EvidenceStrength.STRONG if o.source_kind == SourceKind.PRIMARY else
                        EvidenceStrength.SUPPORTING)
            evidence = Evidence(
                evidence_id="pending", case_id=case.case_id, observation_id=o.observation_id,
                tool=o.tool, source_kind=o.source_kind, claim_type=claim,
                subject=EvidenceSubject(kind=kind, identifier=identifier or o.internal_order_id,
                                        internal_order_id=o.internal_order_id, field=field),
                value=value, event_time=event_time if event_time is not None else o.event_time,
                observed_at=o.observed_at, source_as_of=o.source_as_of,
                source_version=version, protocol_version=protocol,
                completeness=o.completeness, freshness=o.freshness, strength=strength,
                raw_ref=f"observation://{o.observation_id}",
                content_hash=json_hash(o.model_dump(mode="json")), created_at=o.observed_at,
                metadata=EvidenceMetadata(source_path=path, scope=query),
            )
            output.append(evidence.model_copy(update={"evidence_id": "E-" + fingerprint(evidence)}))

        if o.status != ObservationStatus.OK:
            emit(C.SOURCE_LOOKUP_STATUS, o.status.value, "/status")
            return output
        if isinstance(data, FundData):
            r = data.record
            subject = dict(kind=S.FUND_REQUEST, identifier=r.fund_request_id)
            emit(C.FUND_BUSINESS_STATUS, r.business_status.value, "/data/record/business_status", **subject)
            emit(C.LOAN_NO_PRESENT, r.loan_no is not None, "/data/record/loan_no", **subject)
            if r.loan_no is not None:
                emit(C.LOAN_NOTE_REFERENCE, r.loan_no, "/data/record/loan_no", **subject)
        elif isinstance(data, PaymentData):
            r = data.record
            emit(C.PAYMENT_FINALITY, r.payment_finality.value, "/data/record/payment_finality",
                 kind=S.FUND_REQUEST, identifier=r.fund_request_id)
            if r.transaction is not None:
                t = r.transaction
                for claim, field in ((C.PAYMENT_TRANSACTION_ID, "transaction_id"),
                                     (C.PAYMENT_AMOUNT, "amount"), (C.PAYMENT_CURRENCY, "currency"),
                                     (C.TRANSACTION_FUND_REQUEST_ID, "fund_request_id")):
                    value = getattr(t, field)
                    emit(claim, value.value if hasattr(value, "value") else value,
                         f"/data/record/transaction/{field}", kind=S.TRANSACTION,
                         identifier=t.transaction_id, event_time=t.event_time)
        elif isinstance(data, (CallbackData, CallbackRawData)):
            r = data.record
            for claim, value, path in (
                (C.CALLBACK_GATEWAY_RECEIVED, True, "received_at"),
                (C.CALLBACK_SIGNATURE_VERIFIED, r.signature_verified, "signature_verified"),
                (C.CALLBACK_PROTOCOL_VERSION, r.protocol_version, "protocol_version"),
            ):
                emit(claim, value, f"/data/record/{path}", kind=S.CALLBACK,
                     identifier=r.event_id, protocol=r.protocol_version)
        elif isinstance(data, MessagesData):
            for index, r in enumerate(data.records):
                subject = dict(kind=S.MESSAGE, identifier=r.message_id, event_time=r.event_time)
                emit(C.MESSAGE_CONSUME_STATUS, r.consume_status.value,
                     f"/data/records/{index}/consume_status", **subject)
                if r.dlq is not None:
                    emit(C.MESSAGE_DLQ, r.dlq, f"/data/records/{index}/dlq", **subject)
                if r.error is not None:
                    emit(C.MESSAGE_ERROR_CODE, r.error.code, f"/data/records/{index}/error/code", **subject)
                    emit(C.MESSAGE_ERROR_FIELD, r.error.field, f"/data/records/{index}/error/field", **subject)
                    emit(C.MESSAGE_EXPECTED_FIELD_TYPE, r.error.expected_type.value,
                         f"/data/records/{index}/error/expected_type", **subject)
                    emit(C.MESSAGE_ACTUAL_FIELD_TYPE, r.error.actual_type.value,
                         f"/data/records/{index}/error/actual_type", **subject)
        elif isinstance(data, ProtocolData):
            r = data.record
            subject = dict(kind=S.PROTOCOL, identifier=f"{r.partner}@{r.protocol_version}",
                           protocol=r.protocol_version, version=r.protocol_version)
            for field, value in sorted(r.field_schema.items()):
                emit(C.PROTOCOL_FIELD_TYPE, value.value, f"/data/record/field_schema/{field}", field=field, **subject)
            for status, meaning in sorted(r.business_semantics.items()):
                emit(C.PROTOCOL_BUSINESS_SEMANTICS, meaning.value,
                     f"/data/record/business_semantics/{status.value}", field=status.value, **subject)
        elif isinstance(data, TraceData):
            emit(C.REQUEST_SENT, data.record.sent, "/data/record/sent")
            emit(C.HTTP_RESPONSE_STATUS, data.record.http_response.value, "/data/record/http_response")
        elif isinstance(data, AssetData):
            emit(C.ASSET_STATUS, data.record.loan_status.value, "/data/record/loan_status")
        elif isinstance(data, GuaranteeData):
            r = data.record
            emit(C.GUARANTEE_STATUS, r.status.value, "/data/record/status",
                 version=str(r.version), protocol=r.protocol_version)
            emit(C.GUARANTEE_VERSION, r.version, "/data/record/version",
                 version=str(r.version), protocol=r.protocol_version)
        elif isinstance(data, AccountingData):
            emit(C.ACCOUNTING_ENTRY_PRESENT, data.actual_entry is not None, "/data/actual_entry")
        elif isinstance(data, DeliveryData):
            emit(C.ASSET_DELIVERY_STATUS, data.record.delivery_status.value, "/data/record/delivery_status")
        elif isinstance(data, LoanNoteData):
            emit(C.LOAN_NO_PRESENT, True, "/data/record/loan_no", kind=S.FUND_REQUEST,
                 identifier=data.record.fund_request_id)
            emit(C.LOAN_NOTE_REFERENCE, data.record.loan_no, "/data/record/loan_no",
                 kind=S.FUND_REQUEST, identifier=data.record.fund_request_id)
        else:
            raise ValueError("unhandled observation type")
        return output
