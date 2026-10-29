"""Build the common usage dashboard for an authorized consumption scope."""

from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.api.exceptions import ForbiddenException
from gpustack.api.tenant import (
    TenantContext,
    assert_cluster_visible,
    cluster_visibility_conditions,
)
from gpustack.routes.dashboard import _resolve_dashboard_scope, get_active_models
from gpustack.schemas.clusters import Cluster
from gpustack.schemas.dashboard_overview import DashboardOption, DashboardOverview
from gpustack.server import dashboard_usage as usage
from gpustack.utils.rollup_tz import resolve_rollup_tz

_overview_cache = TTLCache(maxsize=128, ttl=15)


async def cluster_options(session: AsyncSession, ctx: TenantContext):
    statement = select(Cluster.id, Cluster.name).where(
        Cluster.deleted_at.is_(None), *cluster_visibility_conditions(ctx, Cluster)
    )
    return [
        DashboardOption(id=id, name=name)
        for id, name in (await session.exec(statement.order_by(Cluster.name))).all()
    ]


async def validate_cluster(
    session: AsyncSession, ctx: TenantContext, cluster_id: Optional[int]
) -> None:
    if cluster_id is not None:
        cluster = await Cluster.one_by_id(session, cluster_id)
        assert_cluster_visible(ctx, cluster)


async def get_overview(
    session: AsyncSession,
    ctx: TenantContext,
    period: str = '7d',
    cluster_id: Optional[int] = None,
    consumer_id: Optional[int] = None,
) -> DashboardOverview:
    """Aggregate usage while preserving caller and deployment ownership scope."""
    owner_id = _resolve_dashboard_scope(ctx)
    if owner_id is not None:
        if consumer_id is not None and consumer_id != owner_id:
            raise ForbiddenException(message='Cannot widen dashboard scope')
        consumer_id = owner_id
    await usage.begin_snapshot(session)
    await validate_cluster(session, ctx, cluster_id)
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
    users_by_tokens = await usage.query_users(session, source, 'tokens')
    users_by_requests = await usage.query_users(session, source, 'requests')
    routes_by_tokens = await usage.query_routes(session, source, 'tokens')
    routes_by_requests = await usage.query_routes(session, source, 'requests')
    route_count = await usage.query_route_count(session, source)
    clusters = await cluster_options(session, ctx)
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
        routes_by_tokens=routes_by_tokens,
        routes_by_requests=routes_by_requests,
        route_count=route_count,
        users_by_tokens=users_by_tokens,
        users_by_requests=users_by_requests,
        active_models=active_models,
        clusters=clusters,
        unassigned_cluster_requests=unassigned,
    )
    _overview_cache[key] = result
    return result.model_copy(deep=True)
