from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.engine import Engine

from credit_harness.domain.enums import ToolName
from credit_harness.persistence.store import open_engine
from credit_harness.settings import database_url
from credit_harness.simulator.service import (
    AuthenticationError, ObservationService, QueryError, ScopeError,
)
from credit_harness.tools.contracts import (
    DISPATCH_CORRELATION_HEADER, DispatchCorrelationId, Observation, ToolQuery,
)


def create_app(engine: Engine | None = None) -> FastAPI:
    app = FastAPI(title="Credit Simulator Observation Tools", version="0.1.0")
    service = ObservationService(engine if engine is not None else open_engine(database_url()))
    bearer = HTTPBearer(auto_error=False)

    @app.post("/tools/{tool}", response_model=Observation)
    def observe(
        tool: ToolName,
        query: ToolQuery,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        dispatch_correlation_id: Annotated[
            DispatchCorrelationId | None, Header(alias=DISPATCH_CORRELATION_HEADER),
        ] = None,
    ):
        if credentials is None:
            raise HTTPException(status_code=401, detail="tool credential required")
        try:
            return service.observe(credentials.credentials, tool, query,
                                   dispatch_correlation_id=dispatch_correlation_id)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except ScopeError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except QueryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    return app
