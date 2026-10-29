"""Platform usage dashboard with a common, edition-independent contract."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from gpustack.api.tenant import resolve_tenant_context
from gpustack.schemas.dashboard_overview import DashboardOverview, DashboardQuery
from gpustack.server.dashboard_overview import get_overview
from gpustack.server.deps import CurrentAdminUserDep, SessionDep

router = APIRouter()


@router.get('/overview', response_model=DashboardOverview)
async def overview(
    session: SessionDep,
    user: CurrentAdminUserDep,
    request: Request,
    query: Annotated[DashboardQuery, Query()],
) -> DashboardOverview:
    ctx = await resolve_tenant_context(request, user)
    return await get_overview(session, ctx, query.period, query.cluster_id)
