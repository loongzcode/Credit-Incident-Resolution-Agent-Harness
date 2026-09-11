from typing import Annotated
from pydantic import Field
from credit_harness.domain.models import Model
from credit_harness.domain.enums import LoanStatus
from credit_harness.evidence.models import ClaimType as C
from .models import EvaluationDimension as D, OutcomePath

EVALUATION_POLICY_VERSION = "1"
VERIFICATION_CONTRACT_VERSION = "1"


class EvaluationPolicy(Model):
    version: str = EVALUATION_POLICY_VERSION
    required_dimensions: tuple[D, ...] = tuple(D)
    evidence_max_age_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
    convergence_grace_seconds: Annotated[int, Field(ge=0, le=3600)] = 30


class CreditGuaranteeDisbursementVerificationContractV1(Model):
    version: str = VERIFICATION_CONTRACT_VERSION
    settled_state: LoanStatus = LoanStatus.SUCCESS
    no_disbursement_state: LoanStatus = LoanStatus.FAILED
    settled_requires_callback: bool = True
    settled_requires_delivery: bool = True

    def exempt_gap(self, gap, path):
        # Existing identity gap asks for a SETTLED transaction. For a proven
        # request-level NO_EFFECT path there is no payment account to match.
        return path == OutcomePath.NO_DISBURSEMENT_PATH and gap.gap_id.endswith(":PAYMENT_IDENTITY")

    def state_expectations(self, path):
        success = path == OutcomePath.SETTLED_PATH
        return ((C.FUND_BUSINESS_STATUS, self.settled_state.value if success else self.no_disbursement_state.value),
                (C.GUARANTEE_STATUS, self.settled_state.value if success else self.no_disbursement_state.value),
                (C.ASSET_STATUS, self.settled_state.value if success else self.no_disbursement_state.value),
                (C.ACCOUNTING_ENTRY_PRESENT, success))
