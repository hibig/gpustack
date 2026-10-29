from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from gpustack.api.auth import get_admin_user
from gpustack.routes import dashboard_overview as route
from gpustack.routes import dashboard as legacy_route
from gpustack.schemas.model_usage import ModelUsage
from gpustack.server.db import get_session


def context(admin=False, org=None):
    return SimpleNamespace(is_platform_admin=admin, current_principal_id=org)


@pytest.mark.asyncio
async def test_legacy_usage_payload_keeps_cluster_attribution_internal(monkeypatch):
    record = ModelUsage(
        id=1,
        date=date(2026, 10, 28),
        model_id=3,
        model_name='model',
        cluster_id=9,
        prompt_token_count=100,
        completion_token_count=20,
        request_count=4,
    )
    monkeypatch.setattr(
        legacy_route, 'get_model_usages', AsyncMock(return_value=[record])
    )
    result = await legacy_route.usage(
        session=None,
        ctx=context(admin=True, org=None),
        start_date=record.date,
        end_date=record.date,
        model_ids=None,
        user_ids=None,
        provider_model_names=None,
    )
    item = result['items'][0]
    assert 'cluster_id' not in item
    assert item['id'] == 1
    assert item['model_id'] == 3
    assert item['date'] == '2026-10-28'
    assert item['prompt_token_count'] == 100
    assert item['completion_token_count'] == 20
    assert item['request_count'] == 4


def test_legacy_deployment_query_preserves_owner_scope_and_usage_filters():
    sql = str(
        legacy_route.active_model_statement(3, 7).compile(
            compile_kwargs={'literal_binds': True}
        )
    )
    assert 'models.cluster_id = 3' in sql
    assert 'models.owner_principal_id = 7' in sql
    assert 'model_usages.consumer_principal_id =' not in sql
    assert 'model_usages.cluster_id =' not in sql


def test_common_openapi_has_no_organization_contract_or_extension_routes():
    app = FastAPI()
    app.include_router(route.router, prefix='/v2/dashboard')
    schema = app.openapi()
    operation = schema['paths']['/v2/dashboard/overview']['get']
    assert {parameter['name'] for parameter in operation['parameters']} == {
        'period',
        'cluster_id',
    }
    assert not any('/enterprise/' in path for path in schema['paths'])
    for name in ('DashboardOverview', 'DashboardRank', 'DashboardQuery'):
        properties = schema['components']['schemas'].get(name, {}).get('properties', {})
        assert not any('organization' in field for field in properties)
        assert 'capabilities' not in properties
    assert 'DashboardCapabilities' not in schema['components']['schemas']


@pytest.mark.asyncio
async def test_unknown_organization_query_is_rejected_before_aggregation(monkeypatch):
    app = FastAPI()
    app.include_router(route.router, prefix='/v2/dashboard')
    app.dependency_overrides[get_admin_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_session] = lambda: None
    aggregate = AsyncMock()
    monkeypatch.setattr(route, 'get_overview', aggregate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test'
    ) as client:
        response = await client.get('/v2/dashboard/overview?organization_id=7')
    assert response.status_code == 422
    assert response.json()['detail'][0]['type'] == 'extra_forbidden'
    aggregate.assert_not_awaited()
