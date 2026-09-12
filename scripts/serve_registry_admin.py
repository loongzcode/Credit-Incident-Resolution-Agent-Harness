"""Run a separately authenticated Registry administration API on loopback.

Local actors use random short-lived synthetic tokens printed only to this terminal.
Production uses configured OIDC access tokens; no passwords or company SSO secrets.
"""
import argparse
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uvicorn
from credit_harness.persistence.store import open_engine, create_schema
from credit_harness.cases.schema import create_harness_schema
from credit_harness.registry.fixtures import synthetic_registry
from credit_harness.registry_admin.identity import (EnterpriseIdentity, LocalIdentityProvider,
    OIDCIdentityProvider, ApplicationAccess, Role)
from credit_harness.registry_admin.service import RegistryAdministration
from credit_harness.registry_admin.api import create_admin_app


def build(local=False):
    if local:
        Path('.local').mkdir(exist_ok=True)
    url = os.environ.get('REGISTRY_DATABASE_URL')
    if not url and not local:
        raise ValueError('REGISTRY_DATABASE_URL required')
    engine = open_engine(url or 'sqlite:///.local/registry-admin.db')
    create_schema(engine); create_harness_schema(engine)
    if local:
        now = datetime.now(timezone.utc)
        tokens, mappings = {}, {}
        for role in Role:
            actor = role.value.removeprefix('REGISTRY_').lower()
            token = secrets.token_urlsafe(32)
            tokens[token] = EnterpriseIdentity(user_id=actor, display_name='Synthetic ' + actor,
                groups=(actor,), authenticated_at=now)
            mappings[actor] = (role,)
            print(f'LOCAL ONLY {actor}: {token}', flush=True)
        identity = LocalIdentityProvider(tokens, expires_at=now + timedelta(hours=2))
    else:
        mappings = json.loads(os.environ['REGISTRY_GROUP_ROLE_MAPPING'])
        identity = OIDCIdentityProvider(issuer=os.environ['REGISTRY_OIDC_ISSUER'],
            audience=os.environ['REGISTRY_OIDC_AUDIENCE'], jwks_url=os.environ['REGISTRY_OIDC_JWKS_URL'])
    service = RegistryAdministration(engine, os.environ.get('REGISTRY_TENANT_ID', 'demo'), ApplicationAccess(mappings))
    if local:
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            empty = service._head(s) is None
        if empty:
            version = service.admin.register(synthetic_registry(service.tenant_id), actor='local-bootstrap')
            service.admin.activate(version, expected_version=None, actor='local-bootstrap')
    origins = tuple(os.environ.get('REGISTRY_ALLOWED_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173').split(',')) if local else tuple(os.environ['REGISTRY_ALLOWED_ORIGINS'].split(','))
    return create_admin_app(service, identity, allowed_origins=origins, local_mode=local)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--local', action='store_true'); parser.add_argument('--port', type=int, default=8002)
    args = parser.parse_args()
    uvicorn.run(build(args.local), host='127.0.0.1', port=args.port)
