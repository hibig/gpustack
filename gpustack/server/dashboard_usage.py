"""Scoped dashboard aggregation over daily usage and boundary-day audit rows.

Whole calendar days use the compact rollup. Partial days use completion-anchored
details, including archived rows, so a comparison never includes a future part
of the previous period. No live model join is used to attribute historical usage.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Tuple

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.sql.selectable import Subquery
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.schemas.dashboard_overview import (
    DashboardBucket,
    DashboardMetrics,
    DashboardRoute,
    DashboardRank,
)
from gpustack.schemas.model_usage import ModelUsage
from gpustack.schemas.model_routes import ModelRoute
from gpustack.schemas.model_usage_details import (
    ModelUsageDetails,
    ModelUsageDetailsArchive,
)
from gpustack.schemas.principals import Principal
from gpustack.utils.rollup_tz import resolve_rollup_tz
from gpustack.utils.usage_snapshots import format_usage_route_label


@dataclass(frozen=True)
class DashboardWindow:
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    buckets: List[Tuple[datetime, datetime]]


def dashboard_window(period: str, now: datetime) -> DashboardWindow:
    """Build calendar-day or rolling-hour windows in the configured timezone."""
    end = now.astimezone(resolve_rollup_tz()).replace(second=0, microsecond=0)
    if period == '24h':
        end_utc = end.astimezone(timezone.utc)
        start_utc = end_utc - timedelta(hours=24)
        start = start_utc.astimezone(end.tzinfo)
        buckets = [
            (
                (start_utc + timedelta(hours=i)).astimezone(end.tzinfo),
                (start_utc + timedelta(hours=i + 1)).astimezone(end.tzinfo),
            )
            for i in range(24)
        ]
        return DashboardWindow(
            start,
            end,
            (start_utc - timedelta(hours=24)).astimezone(end.tzinfo),
            start,
            buckets,
        )
    else:
        days = 7 if period == '7d' else 30
        start = end.replace(hour=0, minute=0) - timedelta(days=days - 1)
        buckets = [
            (start + timedelta(days=i), min(start + timedelta(days=i + 1), end))
            for i in range(days)
        ]
        shift = timedelta(days=days)
    return DashboardWindow(start, end, start - shift, end - shift, buckets)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def usage_conditions(
    table: Any, consumer_id: Optional[int], cluster_id: Optional[int]
) -> List[Any]:
    conditions = []
    if consumer_id is not None:
        conditions.append(table.consumer_principal_id == consumer_id)
    if cluster_id is not None:
        conditions.append(table.cluster_id == cluster_id)
    return conditions


def _projection(table, bucket):
    consumer_name = func.coalesce(table.consumer_name, Principal.name)
    consumer_kind = func.coalesce(
        table.consumer_principal_kind, cast(Principal.kind, String)
    )
    return [
        bucket.label('bucket'),
        table.user_id,
        table.user_name,
        table.consumer_principal_id.label('consumer_id'),
        consumer_name.label('consumer_name'),
        consumer_kind.label('consumer_kind'),
        table.model_route_id.label('route_id'),
        table.model_route_name.label('route_name'),
        table.prompt_token_count.label('input_tokens'),
        table.prompt_cached_token_count.label('input_cached_tokens'),
        table.completion_token_count.label('output_tokens'),
        table.request_count.label('api_requests'),
    ]


def window_source(
    start: datetime,
    end: datetime,
    buckets: List[Tuple[datetime, datetime]],
    consumer_id: Optional[int] = None,
    cluster_id: Optional[int] = None,
) -> Subquery:
    """Return a bounded SQL source without overlapping rollup/detail days."""
    complete, partial = [], []
    hourly = len(buckets) > 1 and _utc(buckets[1][0]) - _utc(
        buckets[0][0]
    ) <= timedelta(hours=1)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        next_day = day + timedelta(days=1)
        lo, hi = max(start, day), min(end, next_day)
        if lo == day and hi == next_day and not hourly:
            complete.append(day.date())
        elif lo < hi:
            partial.append((lo, hi))
        day = next_day

    date_buckets = [
        (ModelUsage.date == lo.date(), i) for i, (lo, _) in enumerate(buckets)
    ]
    rollup = (
        select(*_projection(ModelUsage, case(*date_buckets, else_=-1)))
        .outerjoin(Principal, Principal.id == ModelUsage.consumer_principal_id)
        .where(
            ModelUsage.date.in_(complete),
            *usage_conditions(ModelUsage, consumer_id, cluster_id),
        )
    )
    statements = [rollup]
    for table in (ModelUsageDetails, ModelUsageDetailsArchive):
        anchor = func.coalesce(table.completed_at, table.started_at, table.created_at)
        time_bucket = case(
            *[
                (and_(anchor >= _utc(lo), anchor < _utc(hi)), i)
                for i, (lo, hi) in enumerate(buckets)
            ],
            else_=-1,
        )
        bounds = [and_(anchor >= _utc(lo), anchor < _utc(hi)) for lo, hi in partial]
        statement = (
            select(*_projection(table, time_bucket))
            .outerjoin(Principal, Principal.id == table.consumer_principal_id)
            .where(
                table.date >= start.date(),
                table.date <= end.date(),
                or_(*bounds) if bounds else literal(False),
                *usage_conditions(table, consumer_id, cluster_id),
            )
        )
        statements.append(statement)
    return union_all(*statements).subquery('dashboard_usage')


def lifetime_source(
    consumer_id: Optional[int] = None,
    cluster_id: Optional[int] = None,
    before_date: Optional[date] = None,
) -> Subquery:
    conditions = usage_conditions(ModelUsage, consumer_id, cluster_id)
    if before_date is not None:
        conditions.append(ModelUsage.date < before_date)
    return (
        select(*_projection(ModelUsage, literal(-1)))
        .outerjoin(Principal, Principal.id == ModelUsage.consumer_principal_id)
        .where(*conditions)
        .subquery('lifetime_usage')
    )


def metric_columns(source: Subquery, *, distinct: bool = True) -> List[Any]:
    c = source.c
    columns = [
        func.coalesce(func.sum(c[name]), 0).label(name)
        for name in (
            'input_tokens',
            'input_cached_tokens',
            'output_tokens',
            'api_requests',
        )
    ]
    if distinct:
        columns.append(func.count(func.distinct(c.user_id)).label('active_users'))
    return columns


def metrics(row: Mapping[str, Any]) -> DashboardMetrics:
    values = {
        key: int(row.get(key, 0) or 0)
        for key in DashboardMetrics.model_fields
        if key != 'total_tokens'
    }
    values['total_tokens'] = values['input_tokens'] + values['output_tokens']
    return DashboardMetrics(**values)


async def query_metrics(session: AsyncSession, source: Subquery) -> DashboardMetrics:
    row = (await session.execute(select(*metric_columns(source)))).mappings().one()
    return metrics(row)


async def query_lifetime(
    session: AsyncSession,
    consumer_id: Optional[int],
    cluster_id: Optional[int],
    today_start: datetime,
    today: DashboardMetrics,
) -> Tuple[DashboardMetrics, int]:
    # Reuse today's bounded aggregate so lifetime shares the response cutoff.
    source = lifetime_source(consumer_id, cluster_id, today_start.date())
    row = (
        (await session.execute(select(*metric_columns(source, distinct=False))))
        .mappings()
        .one()
    )
    unassigned = 0
    if cluster_id is not None:
        statement = select(func.coalesce(func.sum(ModelUsage.request_count), 0)).where(
            ModelUsage.cluster_id.is_(None),
            ModelUsage.model_id.is_not(None),
            *usage_conditions(ModelUsage, consumer_id, None),
        )
        unassigned = int((await session.execute(statement)).scalar_one())
    counts = metrics(row)
    for field in (
        'input_tokens',
        'input_cached_tokens',
        'output_tokens',
        'total_tokens',
        'api_requests',
    ):
        setattr(counts, field, getattr(counts, field) + getattr(today, field))
    return counts, unassigned


async def query_buckets(
    session: AsyncSession, source: Subquery, buckets: List[Tuple[datetime, datetime]]
) -> List[DashboardBucket]:
    rows = (
        (
            await session.execute(
                select(source.c.bucket, *metric_columns(source)).group_by(
                    source.c.bucket
                )
            )
        )
        .mappings()
        .all()
    )
    by_index = {r['bucket']: metrics(r) for r in rows}
    return [
        DashboardBucket(
            start=lo, end=hi, **by_index.get(i, DashboardMetrics()).model_dump()
        )
        for i, (lo, hi) in enumerate(buckets)
    ]


async def query_users(
    session: AsyncSession, source: Subquery, metric: str
) -> List[DashboardRank]:
    c = source.c
    order = (
        func.sum(c.input_tokens + c.output_tokens)
        if metric == 'tokens'
        else func.sum(c.api_requests)
    )
    statement = (
        select(
            c.user_id.label('id'),
            func.max(c.user_name).label('name'),
            *metric_columns(source),
        )
        .where(c.user_id.is_not(None))
        .group_by(c.user_id)
        .order_by(order.desc(), c.user_id)
        .limit(10)
    )
    rows = (await session.execute(statement)).mappings().all()
    return [
        DashboardRank(
            id=row['id'], name=row['name'] or 'Unknown', **metrics(row).model_dump()
        )
        for row in rows
    ]


async def query_routes(
    session: AsyncSession, source: Subquery, metric: str
) -> List[DashboardRoute]:
    c = source.c
    order = (
        func.sum(c.input_tokens + c.output_tokens)
        if metric == 'tokens'
        else func.sum(c.api_requests)
    )
    statement = (
        select(c.route_id, c.route_name, *metric_columns(source))
        .group_by(c.route_id, c.route_name)
        .order_by(order.desc(), c.route_id, c.route_name)
        .limit(10)
    )
    rows = (await session.execute(statement)).mappings().all()
    ids = {r['route_id'] for r in rows if r['route_id'] is not None}
    existing = set()
    if ids:
        existing = set(
            (await session.execute(select(ModelRoute.id).where(ModelRoute.id.in_(ids))))
            .scalars()
            .all()
        )
    return [
        DashboardRoute(
            key='route:' + json.dumps([r['route_id'], r['route_name']]),
            id=r['route_id'],
            name=format_usage_route_label(r['route_name']),
            deleted=(
                bool(r['route_id'] is not None or r['route_name'])
                and r['route_id'] not in existing
            ),
            **metrics(r).model_dump(),
        )
        for r in rows
    ]


async def query_route_count(session: AsyncSession, source: Subquery) -> int:
    groups = select(source.c.route_id, source.c.route_name).distinct().subquery()
    return (
        await session.execute(select(func.count()).select_from(groups))
    ).scalar_one()


async def begin_snapshot(session: AsyncSession) -> None:
    """Keep related aggregates in one stable snapshot when supported."""
    if not session.in_transaction() and session.get_bind().dialect.name in (
        'postgresql',
        'mysql',
    ):
        await session.connection(
            execution_options={'isolation_level': 'REPEATABLE READ'}
        )
