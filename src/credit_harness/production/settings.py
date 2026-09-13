"""Per-process configuration. Parsing is shared; secrets are not shared."""
import base64
import json
import os
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url


class StartupConfigurationError(RuntimeError):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @classmethod
    def from_env(cls):
        try:
            values = {key: os.environ[key.upper()] for key in cls.model_fields if key.upper() in os.environ}
            for key in ("oidc_group_roles", "registry_group_role_mapping", "allowed_origins", "admin_allowed_origins", "required_workers"):
                if key in values:
                    values[key] = json.loads(values[key])
            return cls.model_validate(values)
        except Exception:
            raise StartupConfigurationError("PRODUCTION_CONFIGURATION_INVALID") from None

    def key_bytes(self, name):
        value = getattr(self, name)
        return base64.b64decode(value.get_secret_value()) if value is not None else None

    @field_validator("frame_cursor_hmac_key", "frame_cursor_previous_key", "identity_alias_hmac_key", check_fields=False)
    @classmethod
    def key(cls, value):
        if value is not None:
            try:
                if len(base64.b64decode(value.get_secret_value(), validate=True)) < 32:
                    raise ValueError()
            except Exception:
                raise ValueError("base64 key with at least 32 random bytes required") from None
        return value


class DatabaseSettings(Settings):
    database_url: SecretStr
    tenant: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_pool_recycle: int = Field(default=900, ge=30, le=7200)
    statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)

    @model_validator(mode="after")
    def database(self):
        url = make_url(self.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError("PostgreSQL psycopg database required")
        return self


class OIDCSettings(DatabaseSettings):
    oidc_issuer: str
    oidc_jwks_url: str

    @field_validator("oidc_issuer", "oidc_jwks_url")
    @classmethod
    def https(cls, value):
        from urllib.parse import urlparse
        url = urlparse(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("HTTPS endpoint required")
        return value

    @field_validator("allowed_origins", "admin_allowed_origins", check_fields=False)
    @classmethod
    def origins(cls, values):
        from urllib.parse import urlparse
        for value in values:
            cls.https(value)
            url = urlparse(value)
            if url.path not in ("", "/") or url.query or url.fragment:
                raise ValueError("exact origin required")
        return values


class InvestigationApiSettings(OIDCSettings):
    oidc_audience: str = Field(min_length=1)
    oidc_group_roles: dict[str, tuple[str, ...]]
    frame_cursor_hmac_key: SecretStr
    frame_cursor_previous_key: SecretStr | None = None
    identity_alias_hmac_key: SecretStr
    allowed_origins: tuple[str, ...] = Field(min_length=1)
    frame_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    worker_lease_seconds: int = Field(default=120, ge=30, le=600)
    required_workers: tuple[str, ...] = ("agent", "orchestration", "recovery")

    @model_validator(mode="after")
    def access(self):
        from credit_harness.investigation.identity import InvestigationAccess
        InvestigationAccess(self.oidc_group_roles)
        if not set(self.required_workers) <= {"agent", "orchestration", "recovery", "embedding"}:
            raise ValueError("unknown required worker")
        return self


class RegistryAdminSettings(OIDCSettings):
    registry_admin_audience: str = Field(min_length=1)
    registry_group_role_mapping: dict[str, tuple[str, ...]]
    admin_allowed_origins: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def access(self):
        from credit_harness.registry_admin.identity import ApplicationAccess
        ApplicationAccess(self.registry_group_role_mapping)
        return self


def validate_audience_isolation(api: InvestigationApiSettings, admin: RegistryAdminSettings):
    """Deployment preflight compares configs without distributing either secret set."""
    if api.oidc_audience == admin.registry_admin_audience:
        raise StartupConfigurationError("OIDC_AUDIENCES_MUST_DIFFER")


class WorkerSettings(DatabaseSettings):
    http_timeout_seconds: int = Field(default=30, ge=1, le=120)
    worker_lease_seconds: int = Field(default=120, ge=30, le=600)
    worker_tick_seconds: float = Field(default=2, ge=.1, le=60)
    shutdown_grace_seconds: int = Field(default=35, ge=1, le=120)

    @model_validator(mode="after")
    def lease(self):
        if self.worker_lease_seconds <= self.http_timeout_seconds + self.statement_timeout_ms / 1000 + 10:
            raise ValueError("worker lease must exceed bounded IO and commit allowance")
        return self


class RecoveryWorkerSettings(WorkerSettings):
    pass


class OrchestrationWorkerSettings(WorkerSettings):
    tool_bindings_file: str = Field(min_length=1)


class AgentWorkerSettings(OrchestrationWorkerSettings):
    planner_model: str = Field(min_length=1)
    openai_api_key: SecretStr = Field(min_length=1)
    identity_alias_hmac_key: SecretStr


class EmbeddingProviderSettings(Settings):
    embedding_model: str = Field(min_length=1)
    embedding_dimension: int = Field(ge=1, le=2000)
    openai_api_key: SecretStr = Field(min_length=1)


class EmbeddingWorkerSettings(WorkerSettings, EmbeddingProviderSettings):
    reconcile_seconds: int = Field(default=60, ge=5, le=3600)


WORKER_SETTINGS = {"agent": AgentWorkerSettings, "orchestration": OrchestrationWorkerSettings,
    "recovery": RecoveryWorkerSettings, "embedding": EmbeddingWorkerSettings}
