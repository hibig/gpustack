from datetime import datetime
from enum import Enum
from typing import Dict, Optional
from pydantic import ConfigDict, BaseModel
from sqlmodel import Field, SQLModel, JSON, Column

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import PaginatedList, pydantic_column_type
from typing import List


class UtilizationInfo(BaseModel):
    total: int = Field(default=None)
    used: Optional[int] = Field(default=None)
    utilization_rate: Optional[float] = Field(default=None)


class MemoryInfo(UtilizationInfo):
    is_unified_memory: bool = Field(default=False)
    allocated: Optional[int] = Field(default=None)


class CPUInfo(UtilizationInfo):
    pass


class GPUCoreInfo(UtilizationInfo):
    pass


class GPUMemoryInfo(UtilizationInfo):
    allocated: Optional[int] = Field(default=None)


class SwapInfo(UtilizationInfo):
    pass


class GPUDevice(BaseModel):
    uuid: str = Field(default="")
    name: str = Field(default="")
    vendor: str = Field(default="")
    index: int = Field(default=None)
    core: Optional[GPUCoreInfo] = Field(default=None)
    memory: Optional[GPUMemoryInfo] = Field(default=None)
    temperature: Optional[float] = Field(default=None)  # in celsius


GPUInfo = List[GPUDevice]


class MountPoint(BaseModel):
    name: str = Field(default="")
    mount_point: str = Field(default="")
    mount_from: str = Field(default="")
    total: int = Field(default=None)  # in bytes
    used: int = Field(default=None)
    free: int = Field(default=None)
    available: int = Field(default=None)


FileSystemInfo = List[MountPoint]


class OperatingSystemInfo(BaseModel):
    name: str = Field(default="")
    version: str = Field(default="")


class KernelInfo(BaseModel):
    name: str = Field(default="")
    release: str = Field(default="")
    version: str = Field(default="")
    architecture: str = Field(default="")


class UptimeInfo(BaseModel):
    uptime: float = Field(default=None)  # in seconds
    boot_time: str = Field(default="")


class WorkerStateEnum(str, Enum):
    unknown = "Unknown"
    running = "Running"
    inactive = "Inactive"


class WorkerStatus(BaseModel):
    cpu: CPUInfo | None = Field(sa_column=Column(JSON), default=None)
    memory: MemoryInfo | None = Field(sa_column=Column(JSON), default=None)
    gpu: GPUInfo | None = Field(sa_column=Column(JSON), default=None)
    swap: SwapInfo | None = Field(sa_column=Column(JSON), default=None)
    filesystem: FileSystemInfo | None = Field(sa_column=Column(JSON), default=None)
    os: OperatingSystemInfo | None = Field(sa_column=Column(JSON), default=None)
    kernel: KernelInfo | None = Field(sa_column=Column(JSON), default=None)
    uptime: UptimeInfo | None = Field(sa_column=Column(JSON), default=None)

    model_config = ConfigDict(from_attributes=True)


class WorkerBase(SQLModel):
    name: str = Field(index=True, unique=True)
    hostname: str
    ip: str
    labels: Dict[str, str] = Field(sa_column=Column(JSON), default={})

    state: WorkerStateEnum = WorkerStateEnum.unknown
    status: WorkerStatus | None = Field(
        sa_column=Column(pydantic_column_type(WorkerStatus))
    )


class Worker(WorkerBase, BaseModelMixin, table=True):
    id: int | None = Field(default=None, primary_key=True)


class WorkerCreate(WorkerBase):
    pass


class WorkerUpdate(WorkerBase):
    pass


class WorkerPublic(
    WorkerBase,
):
    id: int
    created_at: datetime
    updated_at: datetime


WorkersPublic = PaginatedList[WorkerPublic]