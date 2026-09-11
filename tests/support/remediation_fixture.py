"""Synthetic READ observation fixtures. Never imported by application runtime.

The deployment fixture extends the trusted projection BEFORE ObservationService
persists/hashes its receipt. Normal S1-S8 projections remain unchanged. This is
not a model-authored Evidence insertion, world update or deployed-version guess.
"""
from credit_harness.tools.contracts import MessagesData, ConsumerDeploymentObservation


def install_deployment_observation(monkeypatch, *, version="2.3", protocol="2.3", field_type="string"):
    from credit_harness.simulator import service
    original = service.project
    def project(world, tool, query, now):
        data, event_time = original(world, tool, query, now)
        if isinstance(data, MessagesData):
            data = MessagesData(records=tuple(r.model_copy(update={"consumer_deployment": ConsumerDeploymentObservation(
                event_time=now, schema_version=version, accepted_protocol_version=protocol,
                loan_no_type=field_type)}) for r in data.records))
        return data, event_time
    monkeypatch.setattr(service, "project", project)
