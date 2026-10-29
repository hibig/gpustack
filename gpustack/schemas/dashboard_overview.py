"""Usage-focused dashboard response contracts."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from gpustack.schemas.dashboard import ModelSummary


class DashboardMetrics(BaseModel):
    input_tokens: int = 0
    input_cached_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    api_requests: int = 0
    active_users: int = 0


class DashboardBucket(DashboardMetrics):
    start: datetime
    end: datetime


class DashboardRank(DashboardMetrics):
    id: Optional[int] = None
    name: str


class DashboardRoute(DashboardMetrics):
    key: str
    id: Optional[int] = None
    name: str
    deleted: bool = False


class DashboardOption(BaseModel):
    id: int
    name: str


class DashboardQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')

    period: Literal['24h', '7d', '30d'] = '7d'
    cluster_id: Optional[int] = Field(default=None, gt=0)


class DashboardOverview(BaseModel):
    period: Literal['24h', '7d', '30d']
    timezone: str
    generated_at: datetime
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    current: DashboardMetrics
    previous: DashboardMetrics
    today: DashboardMetrics
    lifetime: DashboardMetrics
    buckets: List[DashboardBucket]
    routes_by_tokens: List[DashboardRoute]
    routes_by_requests: List[DashboardRoute]
    route_count: int
    users_by_tokens: List[DashboardRank]
    users_by_requests: List[DashboardRank]
    active_models: List[ModelSummary]
    clusters: List[DashboardOption]
    unassigned_cluster_requests: int = 0
