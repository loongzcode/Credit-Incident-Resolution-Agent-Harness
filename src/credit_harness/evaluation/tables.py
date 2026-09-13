from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class EvaluationReportRow(Base):
    __tablename__ = "evaluation_reports"
    evaluation_run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class CaseClosureRow(Base):
    __tablename__ = "case_verified_closures"
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), primary_key=True)
    closure_id: Mapped[str] = mapped_column(String(64), unique=True)
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_reports.evaluation_run_id"))
    report_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


def create_evaluation_schema(engine):
    from credit_harness.production.schema import runtime_managed
    if runtime_managed(engine):
        return  # schema version is validated by the production startup gate
    Base.metadata.create_all(engine, tables=[EvaluationReportRow.__table__, CaseClosureRow.__table__])
