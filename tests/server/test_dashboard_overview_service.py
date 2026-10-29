from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gpustack.api.exceptions import ForbiddenException
from gpustack.schemas.dashboard_overview import DashboardMetrics, DashboardRank
from gpustack.schemas.principals import OrgRole
from gpustack.server import dashboard_overview as service


def context(org=None, admin=True, role=OrgRole.OWNER):
    return SimpleNamespace(
        is_platform_admin=admin,
        current_principal_id=org,
        org_role=role,
        user=SimpleNamespace(id=1),
        accessible_cluster_ids=[],
    )


@pytest.mark.asyncio
async def test_common_service_does_not_allow_a_pinned_scope_to_be_widened():
    with pytest.raises(ForbiddenException):
        await service.get_overview(None, context(org=7), consumer_id=8)
    with pytest.raises(ForbiddenException):
        await service.get_overview(
            None, context(org=7, admin=False, role=OrgRole.MEMBER)
        )


@pytest.mark.asyncio
async def test_common_statistics_cache_and_aggregate_scope(monkeypatch):
    service._overview_cache.clear()
    monkeypatch.setattr(
        service,
        'datetime',
        SimpleNamespace(
            now=lambda _: datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
        ),
    )
    counts = DashboardMetrics(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        api_requests=5,
        active_users=1,
    )
    monkeypatch.setattr(service.usage, 'begin_snapshot', AsyncMock())
    query_metrics = AsyncMock(return_value=counts)
    monkeypatch.setattr(service.usage, 'query_metrics', query_metrics)
    monkeypatch.setattr(
        service.usage, 'query_lifetime', AsyncMock(return_value=(counts, 0))
    )
    monkeypatch.setattr(service.usage, 'query_buckets', AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service.usage,
        'query_users',
        AsyncMock(return_value=[DashboardRank(id=1, name='User')]),
    )
    monkeypatch.setattr(service.usage, 'query_routes', AsyncMock(return_value=[]))
    monkeypatch.setattr(service.usage, 'query_route_count', AsyncMock(return_value=2))
    monkeypatch.setattr(service, 'cluster_options', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'get_active_models', AsyncMock(return_value=[]))
    try:
        common = await service.get_overview(None, context())
        assert common.current == counts
        assert common.route_count == 2
        assert not any('organization' in key for key in common.model_dump())
        assert not hasattr(common.users_by_tokens[0], 'organizations')
        sql = str(
            query_metrics.await_args_list[0]
            .args[1]
            .select()
            .compile(compile_kwargs={'literal_binds': True})
        )
        assert 'consumer_principal_id =' not in sql
        assert "principals.kind = 'org'" not in sql
        cached = await service.get_overview(None, context())
        assert cached == common
        assert query_metrics.await_count == 3
        await service.get_overview(None, context(), consumer_id=7)
        assert query_metrics.await_count == 6
        sql = str(
            query_metrics.await_args_list[3]
            .args[1]
            .select()
            .compile(compile_kwargs={'literal_binds': True})
        )
        assert 'model_usages.consumer_principal_id = 7' in sql
    finally:
        service._overview_cache.clear()
