"""Usage-focused dashboard response contracts."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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
    kind: Optional[str] = None
    organizations: List[str] = Field(default_factory=list)


class DashboardModel(DashboardMetrics):
    key: str
    id: Optional[int] = None
    provider_id: Optional[int] = None
    provider_name: Optional[str] = None
    name: str


class DashboardOption(BaseModel):
    id: int
    name: str


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
    models_by_tokens: List[DashboardModel]
    models_by_requests: List[DashboardModel]
    model_count: int
    organizations_by_tokens: List[DashboardRank]
    organizations_by_requests: List[DashboardRank]
    users_by_tokens: List[DashboardRank]
    users_by_requests: List[DashboardRank]
    organization_count: int
    active_models: List[ModelSummary]
    organizations: List[DashboardOption]
    clusters: List[DashboardOption]
    unassigned_cluster_requests: int = 0
