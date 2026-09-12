"""Deterministic typed diff. No secret values or arbitrary JSON diff sent to UI."""
from .models import VersionDiff, DiffEntry


def version_diff(before, after):
    result, risk, scopes, statuses = {}, set(), [], []
    for name, field, key in (("systems", "systems", lambda x: x["system_id"]),
            ("capabilities", "capabilities", lambda x: x["capability_id"]),
            ("authority", "authority_rules", lambda x: x["capability_id"] + ":" + x["claim_type"])):
        old, new = ({key(x): x for x in d.model_dump(mode="json")[field]} for d in (before, after))
        result[name + "_added"] = tuple(sorted(new.keys() - old.keys()))
        result[name + "_removed"] = tuple(sorted(old.keys() - new.keys()))
        changed = []
        for identity in sorted(old.keys() | new.keys()):
            a, b = old.get(identity, {}), new.get(identity, {})
            fields = tuple(sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k)))
            if not fields:
                continue
            if a and b:
                def safe_values(item):
                    return {k: ("[MASKED]" if item.get(k) else None) if k == "credential_ref" else item.get(k) for k in fields}
                changed.append(DiffEntry(entity_id=identity, fields=fields,
                                        before_values=safe_values(a), after_values=safe_values(b)))
            if "scope" in fields:
                scopes.append(name + ":" + identity)
                risk.add("ROUTING_SCOPE_CHANGE")
            if "status" in fields:
                statuses.append(name + ":" + identity)
            if a and (not b or b.get("status") == "DISABLED") and name == "systems":
                risk.add("SYSTEM_DISABLE_OR_REMOVE")
            if name == "capabilities" and b.get("read_or_write") == "WRITE":
                risk.add("WRITE_CAPABILITY_CHANGE")
            if "identity_binding_required" in fields:
                risk.add("IDENTITY_BINDING_CHANGE")
            if name == "authority":
                if (a.get("claim_type") == "PAYMENT_FINALITY" or b.get("claim_type") == "PAYMENT_FINALITY"):
                    risk.add("PAYMENT_FINALITY_AUTHORITY_CHANGE")
                if b.get("authority_level") == "AUTHORITATIVE" and a.get("authority_level") != "AUTHORITATIVE":
                    risk.add("AUTHORITY_UPGRADE")
            # Changing an authoritative payment source's adapter/scope/system is also high risk.
            payment_caps = {r.capability_id for d in (before, after) for r in d.authority_rules
                            if r.claim_type.value == "PAYMENT_FINALITY" and r.authority_level.value == "AUTHORITATIVE"}
            payment_systems = {c.system_id for d in (before, after) for c in d.capabilities if c.capability_id in payment_caps}
            if (name == "capabilities" and identity in payment_caps) or (name == "systems" and identity in payment_systems):
                risk.add("PAYMENT_FINALITY_SOURCE_CHANGE")
        result[name + "_changed"] = tuple(changed)
    return VersionDiff(**result, routing_scope_changed=tuple(sorted(scopes)), status_changed=tuple(sorted(statuses)),
        risk_level="HIGH_RISK" if risk else "STANDARD", risk_reasons=tuple(sorted(risk)))
