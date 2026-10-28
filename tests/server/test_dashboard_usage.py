from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import mysql, postgresql

from gpustack.schemas.model_usage import ModelUsage
from gpustack.schemas.dashboard_overview import DashboardMetrics, DashboardRank
from gpustack.schemas.model_usage_details import (
    ModelUsageDetails,
    ModelUsageDetailsArchive,
)
from gpustack.server import dashboard_usage as usage
from gpustack.server.metrics_collector import (
    ModelUsageMetrics,
    _make_buffer_key,
    create_or_update_model_usage,
)


@pytest.fixture(autouse=True)
def timezone_and_cache(monkeypatch):
    monkeypatch.setattr(usage, 'resolve_rollup_tz', lambda: ZoneInfo('Asia/Shanghai'))


@pytest.mark.parametrize(
    'period,buckets,first',
    [
        ('24h', 24, '2026-10-26T14:32:00+08:00'),
        ('7d', 7, '2026-10-21T00:00:00+08:00'),
        ('30d', 30, '2026-09-28T00:00:00+08:00'),
    ],
)
def test_windows_share_a_cutoff_and_previous_clock_time(period, buckets, first):
    w = usage.dashboard_window(
        period, datetime(2026, 10, 27, 6, 32, 59, tzinfo=timezone.utc)
    )
    assert w.start.isoformat() == first
    assert len(w.buckets) == buckets
    assert w.buckets[-1][1] == w.end
    assert w.previous_end.hour == w.end.hour
    assert w.previous_end.minute == w.end.minute
    assert w.previous_end - w.previous_start == w.end - w.start
    assert all(a[1] == b[0] for a, b in zip(w.buckets, w.buckets[1:]))


@pytest.mark.parametrize('dialect', [postgresql.dialect(), mysql.dialect()])
def test_boundaries_archives_and_scopes_compile_for_supported_dialects(dialect):
    w = usage.dashboard_window('7d', datetime(2026, 10, 27, 6, 32, tzinfo=timezone.utc))
    source = usage.window_source(w.start, w.end, w.buckets, consumer_id=7, cluster_id=3)
    compiled = source.select().compile(
        dialect=dialect, compile_kwargs={'literal_binds': True}
    )
    sql = str(compiled)
    for table in ('model_usages', 'model_usage_details', 'model_usage_details_archive'):
        assert f'{table}.consumer_principal_id = 7' in sql
        assert f'{table}.cluster_id = 3' in sql
    assert "2026-10-26 16:00:00" in sql
    assert "2026-10-27 06:32:00" in sql
    assert (
        'coalesce(model_usage_details.completed_at, '
        'model_usage_details.started_at, model_usage_details.created_at)' in sql
    )
    assert 'model_usages.date IN' in sql
    assert 'UNION ALL' in sql
    # Attribution survives a model moving or being deleted.
    assert 'JOIN models' not in sql


@pytest.mark.asyncio
async def test_summary_preserves_cached_input_without_double_counting():
    row = dict(
        input_tokens=100,
        input_cached_tokens=60,
        output_tokens=20,
        api_requests=9,
        active_users=3,
    )
    result = Mock()
    result.mappings.return_value.one.return_value = row
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    counts = await usage.query_metrics(session, usage.lifetime_source(7))
    assert counts.total_tokens == 120
    assert counts.input_tokens - counts.input_cached_tokens == 40
    assert counts.api_requests == 9
    assert counts.active_users == 3


@pytest.mark.asyncio
async def test_lifetime_uses_completed_days_and_the_same_today_cutoff():
    result = Mock()
    result.mappings.return_value.one.return_value = dict(
        input_tokens=100, input_cached_tokens=60, output_tokens=20, api_requests=9
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    today = DashboardMetrics(
        input_tokens=10,
        input_cached_tokens=6,
        output_tokens=2,
        total_tokens=12,
        api_requests=1,
    )
    start = datetime(2026, 10, 27, tzinfo=ZoneInfo('Asia/Shanghai'))
    counts, unknown = await usage.query_lifetime(session, 7, None, start, today)
    sql = str(
        session.execute.call_args.args[0].compile(
            compile_kwargs={'literal_binds': True}
        )
    )
    assert "model_usages.date < '2026-10-27'" in sql
    assert 'model_usages.consumer_principal_id = 7' in sql
    assert counts.total_tokens == 132
    assert counts.input_cached_tokens == 66
    assert counts.api_requests == 10
    assert unknown == 0


@pytest.mark.asyncio
async def test_empty_buckets_remain_visible_and_do_not_inflate_activity():
    result = Mock()
    result.mappings.return_value.all.return_value = [
        dict(bucket=1, input_tokens=5, api_requests=1, active_users=1)
    ]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    buckets = [
        (start, start + timedelta(hours=1)),
        (start + timedelta(hours=1), start + timedelta(hours=2)),
    ]
    points = await usage.query_buckets(session, usage.lifetime_source(), buckets)
    assert [p.total_tokens for p in points] == [0, 5]
    assert [p.active_users for p in points] == [0, 1]


@pytest.mark.asyncio
async def test_top_users_group_by_id_and_filter_organizations_before_limiting():
    result = Mock()
    result.mappings.return_value.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    source = usage.lifetime_source(7)
    await usage.query_ranking(session, source, 'user', 'tokens')
    sql = str(session.execute.call_args.args[0])
    assert 'GROUP BY lifetime_usage.user_id' in sql
    assert 'GROUP BY lifetime_usage.user_name' not in sql
    await usage.query_ranking(session, source, 'organization', 'requests')
    sql = str(session.execute.call_args.args[0])
    assert 'organization_kind' in sql
    assert 'ORDER BY sum(lifetime_usage.api_requests) DESC' in sql
    assert 'LIMIT' in sql


@pytest.mark.asyncio
async def test_user_organization_labels_exclude_personal_consumers():
    result = Mock()
    result.all.return_value = [(4, 'Example organization')]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    ranks = [DashboardRank(id=4, name='Example user')]
    await usage.query_user_organizations(session, usage.lifetime_source(), ranks)
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={'literal_binds': True}))
    assert "lifetime_usage.organization_kind = 'org'" in sql
    assert 'principals.kind' in sql
    assert ranks[0].organizations == ['Example organization']


def test_cluster_snapshots_partition_the_ingest_buffer():
    a = ModelUsageMetrics(model='m', model_id=1, user_id=2, cluster_id=3)
    b = a.model_copy(update={'cluster_id': 4})
    assert _make_buffer_key(a) != _make_buffer_key(b)


@pytest.mark.asyncio
async def test_rollup_lookup_includes_the_request_cluster(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ModelUsage, 'one_by_fields', lookup)
    save = AsyncMock()
    monkeypatch.setattr(ModelUsage, 'save', save)
    metric = ModelUsage(
        model_id=1,
        model_name='m',
        cluster_id=3,
        date=datetime(2026, 10, 27).date(),
        prompt_token_count=2,
        completion_token_count=1,
        request_count=1,
    )
    await create_or_update_model_usage(Mock(), metric)
    assert lookup.call_args.kwargs['fields']['cluster_id'] == 3


def test_hot_and_archive_detail_shapes_remain_aligned():
    assert {c.name: str(c.type) for c in ModelUsageDetails.__table__.columns} == {
        c.name: str(c.type) for c in ModelUsageDetailsArchive.__table__.columns
    }


def test_hourly_midnight_window_does_not_use_a_daily_rollup():
    w = usage.dashboard_window(
        '24h', datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)
    )
    source = usage.window_source(w.start, w.end, w.buckets)
    compiled = source.select().compile(dialect=postgresql.dialect())
    assert [] in compiled.params.values()
    assert w.end.hour == 0
    assert len(w.buckets) == 24


@pytest.mark.parametrize(
    'now',
    [
        datetime(2026, 3, 8, 16, tzinfo=timezone.utc),
        datetime(2026, 11, 1, 17, tzinfo=timezone.utc),
    ],
)
def test_rolling_hours_are_elapsed_hours_across_dst(monkeypatch, now):
    monkeypatch.setattr(
        usage, 'resolve_rollup_tz', lambda: ZoneInfo('America/New_York')
    )
    w = usage.dashboard_window('24h', now)
    assert w.end.astimezone(timezone.utc) - w.start.astimezone(
        timezone.utc
    ) == timedelta(hours=24)
    assert all(
        hi.astimezone(timezone.utc) - lo.astimezone(timezone.utc) == timedelta(hours=1)
        for lo, hi in w.buckets
    )


def test_deployment_usage_filters_the_consumer_and_historical_cluster():
    from gpustack.routes.dashboard import active_model_statement

    statement = active_model_statement(3, None, 7, filter_usage_cluster=True)
    sql = str(statement.compile(compile_kwargs={'literal_binds': True}))
    assert 'model_usages.consumer_principal_id = 7' in sql
    assert 'model_usages.cluster_id = 3' in sql
    assert 'models.owner_principal_id = 7' not in sql
