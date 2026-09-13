"""Tenant-domain-separated aliases. Production supplies a secret, never SHA(ref)."""
from contextlib import contextmanager
from contextvars import ContextVar
from hmac import digest
from secrets import token_bytes

_local_key = token_bytes(32)  # synthetic/local constructors only
_scope = ContextVar("investigation_alias_scope", default=("local", _local_key))


@contextmanager
def alias_scope(tenant_id, key=None):
    actual = key if key is not None else _local_key
    if len(actual) < 32:
        raise ValueError("alias key must contain at least 32 bytes")
    token = _scope.set((tenant_id, actual))
    try:
        yield
    finally:
        _scope.reset(token)


def alias(value, prefix="REF"):
    tenant, key = _scope.get()
    tenant_key = digest(key, ("tenant:" + tenant).encode(), "sha256")
    return prefix + "-" + digest(tenant_key, (prefix + "\0" + str(value)).encode(), "sha256").hex()[:20]
