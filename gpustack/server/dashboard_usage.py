"""Scoped dashboard aggregation over daily usage and boundary-day audit rows.

Whole calendar days use the compact rollup. Partial days use completion-anchored
details, including archived rows, so a comparison never includes a future part
of the previous period. No live model join is used to attribute historical usage.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Tuple

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.sql.selectable import Subquery
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.schemas.dashboard_overview import (
    DashboardBucket,
    DashboardMetrics,
    DashboardModel,
    DashboardRank,
)
from gpustack.schemas.model_usage import ModelUsage
from gpustack.schemas.model_usage_details import (
    ModelUsageDetails,
    ModelUsageDetailsArchive,
)
from gpustack.schemas.principals import Principal, PrincipalType
from gpustack.utils.rollup_tz import resolve_rollup_tz


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
    organization_name = func.coalesce(table.consumer_name, Principal.name)
    organization_kind = func.coalesce(
        table.consumer_principal_kind, cast(Principal.kind, String)
    )
    model_key = case(
        (table.model_id.is_not(None), literal('model:') + cast(table.model_id, String)),
        else_=literal('provider:')
        + func.coalesce(cast(table.provider_id, String), '')
        + ':'
        + table.model_name,
    )
    return [
        bucket.label('bucket'),
        table.user_id,
        table.user_name,
        table.consumer_principal_id.label('organization_id'),
        organization_name.label('organization_name'),
        organization_kind.label('organization_kind'),
        table.model_id,
        table.model_name,
        table.provider_id,
        table.provider_name,
        model_key.label('model_key'),
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


async def query_ranking(
    session: AsyncSession, source: Subquery, dimension: str, metric: str
) -> List[DashboardRank]:
    c = source.c
    identity, name = c[f'{dimension}_id'], c[f'{dimension}_name']
    statement = select(
        identity.label('id'), func.max(name).label('name'), *metric_columns(source)
    ).where(identity.is_not(None))
    if dimension == 'organization':
        statement = statement.where(
            or_(
                c.organization_kind == PrincipalType.ORG.value,
                c.organization_id.in_(
                    select(Principal.id).where(Principal.kind == PrincipalType.ORG)
                ),
            )
        )
    order = (
        func.sum(c.input_tokens + c.output_tokens)
        if metric == 'tokens'
        else func.sum(c.api_requests)
    )
    statement = statement.group_by(identity).order_by(order.desc(), identity).limit(10)
    rows = (await session.execute(statement)).mappings().all()
    return [
        DashboardRank(
            id=r['id'], name=r['name'] or 'Unknown', **metrics(r).model_dump()
        )
        for r in rows
    ]


async def query_user_organizations(
    session: AsyncSession, source: Subquery, ranks: List[DashboardRank]
) -> None:
    ids = {r.id for r in ranks}
    if not ids:
        return
    c = source.c
    rows = (
        await session.execute(
            select(c.user_id, c.organization_name)
            .where(
                c.user_id.in_(ids),
                or_(
                    c.organization_kind == PrincipalType.ORG.value,
                    c.organization_id.in_(
                        select(Principal.id).where(Principal.kind == PrincipalType.ORG)
                    ),
                ),
            )
            .distinct()
        )
    ).all()
    names = {}
    for user_id, name in rows:
        if name:
            names.setdefault(user_id, set()).add(name)
    for rank in ranks:
        rank.organizations = sorted(names.get(rank.id, []))


async def query_models(
    session: AsyncSession, source: Subquery, metric: str
) -> List[DashboardModel]:
    c = source.c
    columns = [
        func.max(c[key]).label(key)
        for key in ('model_id', 'model_name', 'provider_id', 'provider_name')
    ]
    order = (
        func.sum(c.input_tokens + c.output_tokens)
        if metric == 'tokens'
        else func.sum(c.api_requests)
    )
    statement = (
        select(c.model_key, *columns, *metric_columns(source))
        .group_by(c.model_key)
        .order_by(order.desc(), c.model_key)
        .limit(5)
    )
    rows = (await session.execute(statement)).mappings().all()
    return [
        DashboardModel(
            key=r['model_key'],
            id=r['model_id'],
            name=r['model_name'],
            provider_id=r['provider_id'],
            provider_name=r['provider_name'],
            **metrics(r).model_dump(),
        )
        for r in rows
    ]


async def query_dimension_counts(
    session: AsyncSession, source: Subquery
) -> Tuple[int, int]:
    c = source.c
    is_org = or_(
        c.organization_kind == PrincipalType.ORG.value,
        c.organization_id.in_(
            select(Principal.id).where(Principal.kind == PrincipalType.ORG)
        ),
    )
    statement = select(
        func.count(func.distinct(c.model_key)),
        func.count(func.distinct(case((is_org, c.organization_id), else_=None))),
    )
    return tuple((await session.execute(statement)).one())
