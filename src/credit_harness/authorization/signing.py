import hashlib
import hmac
import json
import os
from typing import Protocol
from pydantic import ValidationError
from .models import ExecutionCapability, SignedExecutionCapability, AuthorizationError, AuthorizationCode as C


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class CapabilitySigner(Protocol):
    def sign(self, payload: ExecutionCapability) -> SignedExecutionCapability: ...
    def verify(self, token: SignedExecutionCapability) -> ExecutionCapability: ...


class HMACCapabilitySigner:
    def __init__(self):
        secret = os.environ.get("CAPABILITY_SIGNING_SECRET", "")
        if len(secret.encode()) < 32:
            raise ValueError("CAPABILITY_SIGNING_SECRET requires at least 32 bytes")
        self._secret = secret.encode()

    def sign(self, payload):
        payload = ExecutionCapability.model_validate(payload.model_dump())
        signature = hmac.new(self._secret, canonical(payload.model_dump(mode="json")), hashlib.sha256).hexdigest()
        return SignedExecutionCapability(payload=payload, signature=signature)

    def verify(self, token):
        if type(token) is not SignedExecutionCapability:
            raise AuthorizationError(C.UNSIGNED_CAPABILITY)
        try:
            checked = SignedExecutionCapability.model_validate(token.model_dump())
        except (ValidationError, ValueError, TypeError):
            raise AuthorizationError(C.INVALID_CAPABILITY) from None
        expected = self.sign(checked.payload).signature
        if not hmac.compare_digest(expected, checked.signature):
            raise AuthorizationError(C.INVALID_CAPABILITY)
        return checked.payload
