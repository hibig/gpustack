"""Usage dashboard; organization selectors always refer to the consumer."""

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Query
from sqlmodel import select

from gpustack.api.exceptions import ForbiddenException, NotFoundException
from gpustack.api.tenant import assert_cluster_visible, cluster_visibility_conditions
from gpustack.routes.dashboard import _resolve_dashboard_scope, get_active_models
from gpustack.schemas.clusters import Cluster
from gpustack.schemas.dashboard_overview import DashboardOption, DashboardOverview
from gpustack.schemas.principals import Principal, PrincipalType
from gpustack.server import dashboard_usage as usage
from gpustack.server.deps import SessionDep, TenantContextDep
from gpustack.utils.rollup_tz import resolve_rollup_tz

router = APIRouter()
_overview_cache = TTLCache(maxsize=128, ttl=15)


async def _consumer_scope(session, ctx, organization_id):
    owner_id = _resolve_dashboard_scope(ctx)
    if organization_id is not None:
        if owner_id is not None and organization_id != owner_id:
            raise ForbiddenException(message='Cannot select another organization')
        organization = await Principal.one_by_id(session, organization_id)
        if (
            organization is None
            or organization.kind != PrincipalType.ORG
            or organization.deleted_at is not None
        ):
            raise NotFoundException(message='Organization not found')
    return owner_id, organization_id if organization_id is not None else owner_id


async def _options(session, ctx):
    statement = select(Principal.id, Principal.name).where(
        Principal.kind == PrincipalType.ORG, Principal.deleted_at.is_(None)
    )
    if ctx.current_principal_id is not None:
        statement = statement.where(Principal.id == ctx.current_principal_id)
    organizations = (await session.exec(statement.order_by(Principal.name))).all()
    statement = select(Cluster.id, Cluster.name).where(
        Cluster.deleted_at.is_(None), *cluster_visibility_conditions(ctx, Cluster)
    )
    clusters = (await session.exec(statement.order_by(Cluster.name))).all()
    return (
        [DashboardOption(id=id, name=name) for id, name in organizations],
        [DashboardOption(id=id, name=name) for id, name in clusters],
    )


@router.get('/overview', response_model=DashboardOverview)
async def overview(
    session: SessionDep,
    ctx: TenantContextDep,
    period: Literal['24h', '7d', '30d'] = '7d',
    organization_id: Annotated[Optional[int], Query(gt=0)] = None,
    cluster_id: Annotated[Optional[int], Query(gt=0)] = None,
) -> DashboardOverview:
    _resolve_dashboard_scope(ctx)
    # Every aggregate in one response sees the same database snapshot.
    if session.get_bind().dialect.name in ('postgresql', 'mysql'):
        await session.connection(
            execution_options={'isolation_level': 'REPEATABLE READ'}
        )
    owner_id, consumer_id = await _consumer_scope(session, ctx, organization_id)
    if cluster_id is not None:
        cluster = await Cluster.one_by_id(session, cluster_id)
        assert_cluster_visible(ctx, cluster)
    now = datetime.now(timezone.utc)
    window = usage.dashboard_window(period, now)
    key = (
        ctx.user.id,
        ctx.current_principal_id,
        consumer_id,
        cluster_id,
        period,
        window.end,
        tuple(sorted(ctx.accessible_cluster_ids)),
    )
    if key in _overview_cache:
        return _overview_cache[key].model_copy(deep=True)

    source = usage.window_source(
        window.start, window.end, window.buckets, consumer_id, cluster_id
    )
    previous_source = usage.window_source(
        window.previous_start,
        window.previous_end,
        [(window.previous_start, window.previous_end)],
        consumer_id,
        cluster_id,
    )
    today_start = window.end.replace(hour=0, minute=0)
    today_source = usage.window_source(
        today_start, window.end, [(today_start, window.end)], consumer_id, cluster_id
    )
    current = await usage.query_metrics(session, source)
    previous = await usage.query_metrics(session, previous_source)
    today = await usage.query_metrics(session, today_source)
    lifetime, unassigned = await usage.query_lifetime(
        session, consumer_id, cluster_id, today_start, today
    )
    buckets = await usage.query_buckets(session, source, window.buckets)
    organizations_by_tokens = await usage.query_ranking(
        session, source, 'organization', 'tokens'
    )
    organizations_by_requests = await usage.query_ranking(
        session, source, 'organization', 'requests'
    )
    users_by_tokens = await usage.query_ranking(session, source, 'user', 'tokens')
    users_by_requests = await usage.query_ranking(session, source, 'user', 'requests')
    await usage.query_user_organizations(
        session, source, users_by_tokens + users_by_requests
    )
    models_by_tokens = await usage.query_models(session, source, 'tokens')
    models_by_requests = await usage.query_models(session, source, 'requests')
    model_count, organization_count = await usage.query_dimension_counts(
        session, source
    )
    organizations, clusters = await _options(session, ctx)
    active_models = await get_active_models(
        session, cluster_id, owner_id, consumer_id, filter_usage_cluster=True
    )
    zone = getattr(resolve_rollup_tz(), 'key', None)
    offset = window.end.strftime('%z')
    zone = zone or ('UTC' if offset == '+0000' else f'UTC{offset[:3]}:{offset[3:]}')
    result = DashboardOverview(
        period=period,
        timezone=zone,
        generated_at=now,
        start=window.start,
        end=window.end,
        previous_start=window.previous_start,
        previous_end=window.previous_end,
        current=current,
        previous=previous,
        today=today,
        lifetime=lifetime,
        buckets=buckets,
        models_by_tokens=models_by_tokens,
        models_by_requests=models_by_requests,
        model_count=model_count,
        organization_count=organization_count,
        organizations_by_tokens=organizations_by_tokens,
        organizations_by_requests=organizations_by_requests,
        users_by_tokens=users_by_tokens,
        users_by_requests=users_by_requests,
        active_models=active_models,
        organizations=organizations,
        clusters=clusters,
        unassigned_cluster_requests=unassigned,
    )
    _overview_cache[key] = result
    return result.model_copy(deep=True)
