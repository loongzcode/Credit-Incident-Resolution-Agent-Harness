from .models import Case, CaseStatus
from .repository import CaseRepository


class CaseService:
    def __init__(self, repository: CaseRepository):
        self.repository = repository

    def create(self, case: Case, *, tool_credential: str) -> Case:
        """Trusted provisioning only. This method is not an Agent HTTP endpoint."""
        return self.repository.create(case, tool_credential)

    def get(self, case_id: str) -> Case:
        return self.repository.get(case_id)

    def pause(self, case_id: str, status: CaseStatus) -> Case:
        return self.repository.pause(case_id, status)
