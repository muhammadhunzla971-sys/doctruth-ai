"""Structured outputs the model must produce. Anything else is rejected and sent back once for repair."""
from typing import Literal

from pydantic import BaseModel, Field


class SemanticVerdict(BaseModel):
    verdict: Literal["still_true", "contradicted", "incomplete", "unknowable"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)
    evidence_ids: list[str] = Field(default_factory=list)


class RewriteResult(BaseModel):
    after: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class CriticResult(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
