"""Supported deployment entrypoint: validate isolated env files before Compose."""
import os
from pathlib import Path
import subprocess
from credit_harness.production.settings import (InvestigationApiSettings, RegistryAdminSettings,
    WORKER_SETTINGS, validate_audience_isolation, StartupConfigurationError)

PROCESSES = {"api":InvestigationApiSettings,"admin":RegistryAdminSettings,**WORKER_SETTINGS}


def load_file(path, cls, *, optional_embedding=False):
    import json
    # Deliberately finite syntax: literal KEY=value, no interpolation or shell.
    values={}
    allowed=set(cls.model_fields)
    if optional_embedding:
        allowed |= {"embedding_model","embedding_dimension"}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key,separator,value=line.partition("=")
        key=key.strip().lower()
        if not separator or key not in allowed or key in values:
            raise StartupConfigurationError("UNEXPECTED_PROCESS_CONFIG_FIELD")
        value=value.strip()
        if key in ("oidc_group_roles","registry_group_role_mapping","allowed_origins","admin_allowed_origins","required_workers"):
            value=json.loads(value)
        values[key]=value
    return cls.model_validate({k:v for k,v in values.items() if k in cls.model_fields})


def validate_deployment(paths):
    try:
        configs={kind:load_file(paths[kind],cls,optional_embedding=kind=="agent") for kind,cls in PROCESSES.items()}
        validate_audience_isolation(configs["api"],configs["admin"])
        return configs
    except Exception:
        raise StartupConfigurationError("DEPLOYMENT_CONFIGURATION_INVALID") from None


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--check-only",action="store_true")
    args=parser.parse_args()
    validate_deployment({kind:os.environ[kind.upper()+"_ENV_FILE"] for kind in PROCESSES})
    if not args.check_only:
        subprocess.run(["docker","compose","-f","deploy/compose.production.yml","up","-d"],check=True)


if __name__=="__main__":
    try:
        main()
    except Exception:
        raise SystemExit("DEPLOYMENT_FAILED") from None
