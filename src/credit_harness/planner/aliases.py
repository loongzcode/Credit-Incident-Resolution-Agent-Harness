from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping
from copy import deepcopy

from credit_harness.evidence.models import ClaimType as C, SubjectKind

REFERENCE_CLAIMS = {C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID, C.LOAN_NOTE_REFERENCE,
                    C.ASSET_DELIVERY_EVENT_REF}


@dataclass(frozen=True)
class AliasProjection:
    payload: dict
    # Runtime-only result, never part of ModelInputBundle.
    alias_map: Mapping[str, str]


class ReferenceAliasProjector:
    def project(self, snapshot, payload):
        refs = set(snapshot.financial_identity.transaction_ref_preview)
        for fact in snapshot.current_facts:
            if fact.subject.kind != SubjectKind.ORDER:
                refs.add(fact.subject.identifier)
            if fact.claim_type in REFERENCE_CLAIMS:
                refs.add(fact.value)
        for state in snapshot.history_digest.state_transitions:
            if state.subject.kind != SubjectKind.ORDER:
                refs.add(state.subject.identifier)
            if state.claim_type in REFERENCE_CLAIMS:
                refs.update((state.first_observed_value, state.last_observed_value))
        # Reserve every existing literal, so an upstream EXTREF-001 cannot collide.
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from strings(item)
        reserved = set(strings(payload))
        forward = {}
        number = 1
        for ref in sorted(refs):
            while (alias := f"EXTREF-{number:03d}") in reserved:
                number += 1
            forward[ref] = alias
            reserved.add(alias)
        # Project reference-typed locations only: an external identifier literally
        # named SUCCESS must not rewrite a business status or a control parameter.
        projected = deepcopy(payload)
        external = projected["untrusted_external_data"]
        for item in external["current_facts"]:
            if item["subject"]["kind"] != SubjectKind.ORDER:
                item["subject"]["identifier"] = forward[item["subject"]["identifier"]]
            if item["claim_type"] in REFERENCE_CLAIMS:
                item["value"] = forward[item["value"]]
        for item in external["history_digest"]["state_transitions"]:
            if item["subject"]["kind"] != SubjectKind.ORDER:
                item["subject"]["identifier"] = forward[item["subject"]["identifier"]]
            if item["claim_type"] in REFERENCE_CLAIMS:
                for field in ("first_observed_value", "last_observed_value"):
                    item[field] = forward[item[field]]
        identity = projected["deterministic_derived"]["financial_identity"]
        identity["transaction_ref_preview"] = [forward[ref] for ref in identity["transaction_ref_preview"]]
        return AliasProjection(projected, MappingProxyType({alias: ref for ref, alias in forward.items()}))
