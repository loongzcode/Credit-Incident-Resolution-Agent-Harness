import os
from pathlib import Path


def database_url() -> str:
    configured = os.environ.get("SIM_DATABASE_URL")
    if configured:
        return configured
    local = Path(".local").resolve()
    local.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(local / 'simulator.db').as_posix()}"

