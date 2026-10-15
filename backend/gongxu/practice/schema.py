from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PracticeCreate(BaseModel):
    sop_id: str = Field(min_length=1, max_length=32)
    allow_provider_export: bool = False


class CapturedFrame(BaseModel):
    sequence: int = Field(ge=1)
    captured_at_ms: int = Field(ge=0)
    data_uri: str = Field(min_length=32, max_length=600_000)


class ObservationRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=80)
    step_revision: int = Field(ge=1)
    frames: list[CapturedFrame] = Field(min_length=3, max_length=6)


class CheckObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str
    status: Literal["satisfied", "not_yet", "violated", "unknown"]
    evidence_frame_ids: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(default="", max_length=1000)


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[CheckObservation] = Field(default_factory=list, max_length=24)


class PracticeAction(BaseModel):
    action: Literal["pause", "resume", "stop", "skip_optional", "confirm_manual"]
    check_ids: list[str] = Field(default_factory=list, max_length=24)
    reason: str = Field(default="", max_length=1000)
