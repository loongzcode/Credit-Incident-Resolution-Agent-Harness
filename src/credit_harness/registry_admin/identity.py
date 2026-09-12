"""SSO validates identity; application code maps trusted groups to permissions."""
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from pydantic import AwareDatetime, Field
from credit_harness.domain.models import Model


class Permission(StrEnum):
    REGISTRY_VIEW = "REGISTRY_VIEW"
    REGISTRY_EDIT = "REGISTRY_EDIT"
    REGISTRY_SUBMIT = "REGISTRY_SUBMIT"
    REGISTRY_APPROVE = "REGISTRY_APPROVE"
    REGISTRY_ACTIVATE = "REGISTRY_ACTIVATE"
    REGISTRY_ROLLBACK = "REGISTRY_ROLLBACK"
    REGISTRY_AUDIT = "REGISTRY_AUDIT"


class Role(StrEnum):
    REGISTRY_VIEWER = "REGISTRY_VIEWER"
    REGISTRY_EDITOR = "REGISTRY_EDITOR"
    REGISTRY_APPROVER = "REGISTRY_APPROVER"
    REGISTRY_ACTIVATOR = "REGISTRY_ACTIVATOR"
    REGISTRY_AUDITOR = "REGISTRY_AUDITOR"


P = Permission
ROLE_PERMISSIONS = {
    Role.REGISTRY_VIEWER: {P.REGISTRY_VIEW},
    Role.REGISTRY_EDITOR: {P.REGISTRY_VIEW, P.REGISTRY_EDIT, P.REGISTRY_SUBMIT, P.REGISTRY_ROLLBACK},
    Role.REGISTRY_APPROVER: {P.REGISTRY_VIEW, P.REGISTRY_APPROVE},
    Role.REGISTRY_ACTIVATOR: {P.REGISTRY_VIEW, P.REGISTRY_ACTIVATE},
    Role.REGISTRY_AUDITOR: {P.REGISTRY_VIEW, P.REGISTRY_AUDIT},
}


class EnterpriseIdentity(Model):
    user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    groups: tuple[str, ...] = Field(max_length=100)
    authenticated_at: AwareDatetime


class IdentityProvider(Protocol):
    def authenticate(self, bearer: str) -> EnterpriseIdentity: ...


class IdentityError(ValueError):
    pass


class LocalIdentityProvider:
    """Explicit test-only injection. No passwords, no caller-supplied role headers."""
    def __init__(self, tokens: dict[str, EnterpriseIdentity], *, expires_at: datetime):
        self.tokens = {sha256(k.encode()).digest(): v for k, v in tokens.items()}
        self.expires_at = expires_at

    def authenticate(self, bearer):
        identity = self.tokens.get(sha256(bearer.encode()).digest())
        if identity is None or datetime.now(timezone.utc) >= self.expires_at:
            raise IdentityError("UNAUTHENTICATED")
        return identity


class OIDCIdentityProvider:
    """Validate issuer's JWT access tokens for THIS API audience; not browser ID tokens.

    Issuer/JWKS/audience are deployment configuration, never read from token URLs.
    Login / PKCE and token refresh belong to the enterprise SSO client.
    """
    def __init__(self, *, issuer: str, audience: str, jwks_url: str, required_scope="registry.admin"):
        import jwt
        if not issuer.startswith("https://") or not jwks_url.startswith("https://") or not audience:
            raise ValueError("HTTPS issuer/JWKS and API audience required")
        self.issuer, self.audience, self.scope = issuer, audience, required_scope
        self.keys = jwt.PyJWKClient(jwks_url, timeout=5, lifespan=300)

    def authenticate(self, bearer):
        import jwt
        try:
            header = jwt.get_unverified_header(bearer)
            if header.get("alg") != "RS256":
                raise IdentityError("UNAUTHENTICATED")
            key = self.keys.get_signing_key_from_jwt(bearer).key
            data = jwt.decode(bearer, key, algorithms=["RS256"], issuer=self.issuer, audience=self.audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            if self.scope not in data.get("scope", "").split():
                raise IdentityError("UNAUTHENTICATED")
            groups = data.get("groups", [])
            if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
                raise IdentityError("UNAUTHENTICATED")
            return EnterpriseIdentity(user_id=data["sub"], display_name=data.get("name", data["sub"]),
                groups=tuple(groups), authenticated_at=datetime.fromtimestamp(data.get("auth_time", data["iat"]), timezone.utc))
        except Exception:
            # Provider errors must not log tokens or expose validation internals.
            raise IdentityError("UNAUTHENTICATED") from None


class ApplicationAccess:
    def __init__(self, group_roles: dict[str, tuple[Role, ...]]):
        self.mapping = {g: tuple(Role(r) for r in roles) for g, roles in group_roles.items()}

    def roles(self, identity):
        return tuple(sorted({r for g in identity.groups for r in self.mapping.get(g, ())}))

    def permissions(self, identity):
        return tuple(sorted({p for r in self.roles(identity) for p in ROLE_PERMISSIONS[r]}))

    def require(self, identity, permission):
        if permission not in self.permissions(identity):
            from .models import AdminError
            raise AdminError("FORBIDDEN", 403)
