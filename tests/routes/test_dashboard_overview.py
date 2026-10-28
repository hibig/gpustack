from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gpustack.api.exceptions import ForbiddenException
from gpustack.routes import dashboard_overview as route
from gpustack.schemas.principals import OrgRole, PrincipalType


def context(admin=False, org=7, role=OrgRole.OWNER):
    return SimpleNamespace(
        is_platform_admin=admin, current_principal_id=org, org_role=role
    )


@pytest.mark.asyncio
async def test_an_org_owner_cannot_select_another_consumer():
    with pytest.raises(ForbiddenException):
        await route._consumer_scope(None, context(), 8)


@pytest.mark.asyncio
async def test_members_cannot_turn_a_dashboard_query_into_an_org_wide_read():
    with pytest.raises(ForbiddenException):
        await route._consumer_scope(None, context(role=OrgRole.MEMBER), None)


@pytest.mark.asyncio
async def test_pinned_admin_context_cannot_be_widened_by_selector():
    with pytest.raises(ForbiddenException):
        await route._consumer_scope(None, context(admin=True), 8)


@pytest.mark.asyncio
async def test_platform_admin_selects_consumer_without_changing_resource_owner_scope(
    monkeypatch,
):
    monkeypatch.setattr(
        route.Principal,
        'one_by_id',
        AsyncMock(
            return_value=SimpleNamespace(id=8, kind=PrincipalType.ORG, deleted_at=None)
        ),
    )
    owner, consumer = await route._consumer_scope(
        None, context(admin=True, org=None), 8
    )
    assert owner is None
    assert consumer == 8
