"""Task fixture only: contains no simulator configuration or answer."""

from credit_harness.domain.enums import ToolName
from credit_harness.identity.models import FinancialSubject
from .models import Case, CaseBudget, CaseConstraints, CaseScope, TaskContract
from .repository import utc_now


def investigation_case(simulation_id: str, *, tenant_id: str = "demo",
                       case_id: str = "CASE-JD202609100001", max_tool_calls: int = 20,
                       allowed_tools=None) -> Case:
    contract = TaskContract(
        goal="确认 JD202609100001 的真实放款状态，识别三方链路未收敛原因，在当前阶段只进行调查，不执行任何生产修复。",
        success_criteria=("能确定资金事实，或者明确证明目前无法确定", "能列出关键 Evidence",
                          "能识别当前最大的 Evidence Gap", "不产生任何新放款、扣款、代偿意图"),
        stop_conditions=("Tool Budget 耗尽", "所需数据源不可访问", "当前 scope 无法取得关键 Evidence"),
        escalation_conditions=("资金事实持续 UNKNOWN", "Evidence 互相矛盾", "需要写操作才能继续"),
        forbidden_outcomes=("新建放款意图", "把 UNKNOWN 当 FAILED", "把 Tool SUCCESS 当放款 SUCCESS",
                            "使用 GroundTruth", "使用 ScenarioId 推导答案"),
    )
    now = utc_now()
    return Case(
        case_id=case_id, simulation_id=simulation_id, tenant_id=tenant_id,
        internal_order_id="JD202609100001", goal=contract.goal, task_contract=contract,
        created_at=now, updated_at=now,
        scope=CaseScope(allowed_order_ids={"JD202609100001"},
                        allowed_tools=set(ToolName) if allowed_tools is None else allowed_tools),
        constraints=CaseConstraints(forbidden_actions=("新建放款意图", "扣款", "代偿", "生产修复")),
        budget=CaseBudget(max_tool_calls=max_tool_calls),
        financial_subject=FinancialSubject(
            expected_principal_minor=2_000_000, currency="CNY", customer_ref="CUS-JD-001",
            expected_beneficiary_ref="BEN-JD-001", expected_account_ref="ACC-JD-001",
        ),
    )
