from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from ..constants.merge import MAX_BRANCH_LENGTH


class PackageRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    origin_uid: str = Field(max_length=80)
    code: str = Field(max_length=64)
    split: Literal["train", "cal", "test"]
    values: dict
    note: str = Field(default="", max_length=1500)


class PackageFeature(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str
    position: int


class PackageInput(BaseModel):
    # Files exported from this service carry ``exported_at``; a round trip
    # must be importable verbatim, so unknown transport fields are ignored.
    model_config = ConfigDict(extra="ignore")
    format: str = Field(max_length=64)
    package_uid: str = Field(min_length=1, max_length=80)
    study_code: str = Field(min_length=1, max_length=32)
    branch: str = Field(min_length=1, max_length=MAX_BRANCH_LENGTH)
    base_revision: int
    base_hash: str = Field(min_length=8, max_length=128)
    content_hash: str = Field(min_length=8, max_length=128)
    features: list[PackageFeature]
    records: list[PackageRecord]


class PackageOutput(BaseModel):
    id: int
    package_uid: str
    study_id: int
    branch: str
    base_revision: int
    base_hash: str
    content_hash: str
    state_hash: str = ""
    status: str
    summary: dict
    conflicts: list
    resolution: str
    resolved_by: str
    resolved_at: str
    imported_by: str
    created_at: str
    replayed: bool = False


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["ours", "theirs"]
    note: str = Field(default="", max_length=1000)


class BaselineOutput(BaseModel):
    revision: int
    base_hash: str
    branch: str
    created_at: str
    window_count: int
