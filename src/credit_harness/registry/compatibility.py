"""Read historical Step 14 bodies without changing their persisted bytes/hash."""
from copy import deepcopy
from .models import RegistryDefinition, SourceChannel


def read_definition(payload):
    payload = deepcopy(payload)
    for system in payload["systems"]:
        legacy = system["system_type"]
        if legacy in ("FUNDING_CORE", "PAYMENT_LEDGER"):
            system["system_type"] = {"FUNDING_CORE": "FUNDING_INTEGRATION",
                                     "PAYMENT_LEDGER": "PAYMENT_STATUS_SOURCE"}[legacy]
        if "source_channel" not in system:
            # Explicit migration semantics; no inference from display_name/hostname.
            system["source_channel"] = (SourceChannel.PARTNER_OFFICIAL_API if system["system_type"] in
                ("FUNDING_INTEGRATION", "PAYMENT_STATUS_SOURCE", "ASSET_PLATFORM") else SourceChannel.INTERNAL_SYSTEM)
    return RegistryDefinition.model_validate(payload)
