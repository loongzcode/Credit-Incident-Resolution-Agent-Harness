"""Trusted Python instances only. No module loading, URL discovery or secret serialization."""
from types import MappingProxyType
from .models import RegistryError, ResolutionCode


class TrustedAdapterResolver:
    def __init__(self, bindings):
        # (system_id, adapter_id, query_version, response_version) -> factory(case_id)
        self._bindings = MappingProxyType(dict(bindings))

    def resolve(self, resolved):
        key = (resolved.system_id, resolved.adapter_id, resolved.query_contract_version,
               resolved.response_contract_version)
        factory = self._bindings.get(key)
        if factory is None:
            raise RegistryError(ResolutionCode.NO_REGISTERED_SOURCE)
        return factory(resolved.case_id)
