"""Export only browser-safe OpenAPI contracts; no SDK or example payloads."""
import json
from pathlib import Path

from credit_harness.api.ui import create_ui_app

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
    target.write_text(json.dumps(create_ui_app({}).openapi(), indent=2) + "\n", encoding="utf-8")
