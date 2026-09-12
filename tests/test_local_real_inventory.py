"""Opt-in, read-only local parsing. Never copy/upload/print workbook contents."""
import os
import json
from pathlib import Path
from datetime import datetime, timezone
import pytest
from credit_harness.registry_admin.importer import parse_inventory


def local_statistics(path):
    # Keep worksheet content out of pytest assertions and assertion introspection.
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(5_000_001)
        parsed = parse_inventory(content, "local-inventory.xlsx", "local-statistics-only", datetime.now(timezone.utc))
        return dict(source_sheet_count=parsed["source_sheet_count"], source_rows=parsed["source_rows"],
            unique_systems=parsed["unique_systems"], shared_system_count=len(parsed["shared_systems"]),
            expected_shared_codes_present={"aut", "sso"}.issubset(parsed["shared_systems"]),
            file_hash=parsed["source_file_hash"],
            invalid_count=len(parsed["invalid"]), conflict_count=len(parsed["conflicts"]),
            error_codes=sorted({e["code"] for e in parsed["invalid"] + parsed["conflicts"]}))
    except Exception:
        # No file path, data, or parser exception is exposed to the report.
        return {"error_codes": ["LOCAL_INVENTORY_PARSE_FAILED"]}


@pytest.mark.local_inventory
@pytest.mark.skipif(not os.environ.get("LOCAL_REAL_INVENTORY_XLSX"), reason="optional local inventory: LOCAL_REAL_INVENTORY_XLSX not configured")
def test_local_real_inventory_statistics():
    summary = local_statistics(os.environ["LOCAL_REAL_INVENTORY_XLSX"])
    print(json.dumps(summary, ensure_ascii=True))
    assert summary.get("error_codes") == []
    assert summary["source_sheet_count"] == 2
    assert summary["source_rows"] == 66
    assert summary["unique_systems"] == 64
    assert summary["expected_shared_codes_present"]
