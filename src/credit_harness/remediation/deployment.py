"""Current replay compatibility; never proof of failure-time deployment or root cause."""
from credit_harness.domain.enums import SourceKind, ToolName
from credit_harness.evidence.models import ClaimType as C

DEPLOYMENT_CLAIMS = (C.CONSUMER_DEPLOYED_SCHEMA_VERSION, C.CONSUMER_ACCEPTED_PROTOCOL_VERSION,
                     C.CONSUMER_LOAN_NO_FIELD_TYPE)


def deployment_witness(index, message):
    groups = []
    for version in index.current(C.CONSUMER_DEPLOYED_SCHEMA_VERSION):
        if (version.subject != message.subject or version.tool != ToolName.MESSAGES
                or version.metadata.callback_event_id != message.metadata.callback_event_id
                or version.event_time is None or message.event_time is None
                or version.event_time < message.event_time):
            continue
        fields = [tuple(e for e in index.current(c) if e.subject == version.subject
                  and e.observation_id == version.observation_id and e.event_time == version.event_time
                  and e.content_hash == version.content_hash and e.source_kind == SourceKind.PRIMARY
                  and e.tool == ToolName.MESSAGES and e.metadata.callback_event_id == version.metadata.callback_event_id)
                  for c in DEPLOYMENT_CLAIMS]
        if all(len({e.value for e in field}) == 1 for field in fields):
            items = tuple(e for field in fields for e in field)
            if all(len({e.value for e in index.current(c) if e.subject == version.subject}) == 1
                   for c in DEPLOYMENT_CLAIMS):
                groups.append(items)
    return min(groups, key=index.refs) if groups else ()
