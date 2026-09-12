"""Inventory read migration and explicit master/source joins; no authority inference."""
from .models import CompanySystemRecord, AgentConfigurationStatus as Status


def read_records(payload):
    records = []
    for raw in payload:
        data = dict(raw)
        if "memberships" not in data:
            # Step 14.2's 系统分类 field was misnamed system_category. Preserve it
            # as a group; that parser did not independently collect the type label.
            source = data.pop("source_metadata")
            group = data.pop("system_category", "") or source["source_row_ref"].rsplit("!", 1)[0]
            data["system_type_label"] = ""
            data["memberships"] = [dict(system_code=data["system_code"], inventory_group=group, source_metadata=source)]
        records.append(CompanySystemRecord.model_validate(data).model_dump(mode="json"))
    return records


def merge_records(current, incoming):
    merged = {r["system_code"]: r for r in read_records(current)}
    for row in read_records(incoming):
        old = merged.get(row["system_code"])
        if old:
            # Partial imports cannot silently erase another group's membership.
            incoming_groups = {m["inventory_group"] for m in row["memberships"]}
            row = {**row, "memberships": [m for m in old["memberships"] if m["inventory_group"] not in incoming_groups] + row["memberships"]}
        merged[row["system_code"]] = row
    return [merged[k] for k in sorted(merged)]


def content_identity(row):
    return {**{k: row[k] for k in ("system_code", "system_name", "system_type_label", "description", "main_functions")},
            "inventory_groups": sorted({m["inventory_group"] for m in row["memberships"]})}


def join_record(row, definition):
    sources = [s for s in definition["systems"] if s.get("company_system_code") == row["system_code"]]
    source_ids = {s["system_id"] for s in sources}
    caps = [c for c in definition["capabilities"] if c["system_id"] in source_ids]
    cap_ids = {c["capability_id"] for c in caps}
    # Configuration completeness is not runtime eligibility, authority or permission.
    configured = bool(sources) and all(any(c["system_id"] == s["system_id"] for c in caps) for s in sources)
    status = Status.CONFIGURED if configured else (Status.PARTIALLY_CONFIGURED if sources else Status.NOT_CONFIGURED)
    return dict(company_system=row, inventory_groups=sorted({m["inventory_group"] for m in row["memberships"]}),
        agent_configuration_status=status.value, agent_source_count=len(sources), capability_count=len(caps),
        registry_sources=sources, capabilities=caps,
        authority=[r for r in definition["authority_rules"] if r["capability_id"] in cap_ids])
