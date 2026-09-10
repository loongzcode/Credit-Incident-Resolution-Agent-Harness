"""Manual HTTP tool inspection; deliberately no Agent or truth access."""

import argparse
import json
from pathlib import Path

from credit_harness.domain.enums import ToolName
from credit_harness.tools.client import ToolClient
from credit_harness.tools.contracts import ToolQuery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    credentials = json.loads(args.credentials_file.read_text(encoding="utf-8"))
    query = ToolQuery(internal_order_id=credentials["order_id"])
    with ToolClient(args.base_url, credentials["tool_token"]) as client:
        for tool in (ToolName.TRACE, ToolName.PAYMENT, ToolName.CALLBACK, ToolName.MESSAGES):
            print(client.observe(tool, query).model_dump_json(indent=2))


if __name__ == "__main__":
    main()

