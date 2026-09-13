"""Explicit human trace projection; never dump a stored runtime payload."""
import re
from hashlib import sha256
from .models import TraceItem, TraceKind as K, TraceField


def alias(value, prefix="REF"):
    return prefix + "-" + sha256(str(value).encode()).hexdigest()[:20]


def display(value):
    if value is None:
        return "UNKNOWN"
    text = str(value)
    # Free-text audit summaries have no authority. Sensitive-looking text is
    # withheld in full; external identifiers use aliases, not this function.
    if (len(text) > 500 or re.search(r"\d{11,}|(?:https?|vault)://|sk-[A-Za-z0-9]|@|(?:\d{1,3}\.){3}\d|(?:password|secret|api.?key)\s*[:=]", text, re.I)):
        return "REDACTED"
    return text


def fields(payload, names):
    return tuple(TraceField(name=n, value=display(payload[n])) for n in names if n in payload)


def entry(kind, identity, status, payload, names=(), *, at=None, refs=(), related=(), warning=None, historical=False):
    return TraceItem(trace_id=alias(identity, kind.value.upper()), kind=kind, status=display(status),
        occurred_at=at, fields=fields(payload, names), evidence_refs=tuple(refs),
        related_refs=tuple(related), warning=warning, historical=historical)


def planner_items(run):
    result = []
    for turn in run.get("turns", ()):
        for attempt in turn.get("attempts", ()):
            d = attempt["decision"]
            result.append(entry(K.CONTEXT, d["snapshot_id"], "RECORDED", {"snapshot_id": d["snapshot_id"]},
                ("snapshot_id",), at=d["created_at"]))
            candidates = [(v["candidate"], "VALID", ()) for v in d["valid_candidates"]]
            candidates += [(v["candidate"], "REJECTED", v["reason_codes"]) for v in d["rejected_candidates"]]
            selected = (d.get("selected_action") or {}).get("candidate", {}).get("candidate_id")
            ranks = {r["candidate_id"]: r for r in d["ranking_details"]}
            for c, status, reasons in candidates:
                data = {"snapshot_id": d["snapshot_id"], "candidate_id": alias(c["candidate_id"], "CANDIDATE"),
                    "candidate_count": len(candidates), "action_type": c["action_type"],
                    "tool_name": c.get("tool_name"), "target_gaps": ", ".join(c["target_gap_ids"]),
                    "expected_claims": ", ".join(c.get("expected_claim_types", ())),
                    "reason_summary": c.get("reason_summary", c.get("reason_code")),
                    "rejections": ", ".join(reasons), "selected": c["candidate_id"] == selected,
                    "model": d["planner_model_metadata"].get("model_name", "UNKNOWN"),
                    "provider": d["planner_model_metadata"].get("model_provider", "UNKNOWN")}
                for key, value in ranks.get(c["candidate_id"], {}).items():
                    if key != "candidate_id":
                        data["rank_" + key] = value
                result.append(entry(K.PLANNER, d["decision_id"] + c["candidate_id"], status,
                    data, tuple(data), at=d["created_at"]))
            guidance = {"build_status": d.get("guidance_build_status", "EMPTY"),
                "degradation": d.get("guidance_degradation", "NONE"),
                "skills": ", ".join(display(s["skill_id"]) + "@" + display(s["version"]) for s in d.get("skill_refs", ())),
                "experiences": ", ".join(alias(e, "EXPERIENCE") for e in d.get("experience_refs", ()))}
            result.append(entry(K.KNOWLEDGE, d["decision_id"], "RECORDED", guidance, tuple(guidance),
                at=d["created_at"], historical=True, warning="HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE"))
        selected = (turn.get("selected_action") or {}).get("candidate", {})
        if turn.get("turn_outcome") in ("WAITING", "ESCALATED"):
            result.append(entry(K.WAIT if turn["turn_outcome"] == "WAITING" else K.ESCALATE,
                run["run_id"] + str(turn["turn_number"]), turn["turn_outcome"], selected,
                ("reason_code", "suggested_wait_seconds")))
    return result
