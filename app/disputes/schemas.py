from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """证据材料对某一用途、指标和适用人群声明的结论方向。"""

    intended_use: str = Field(min_length=2, max_length=2000, description="用途，与产品登记用途或其具体场景一致")
    metric: str = Field(min_length=1, max_length=120, description="性能或临床指标，如 sensitivity、假阳性率")
    population: str = Field(min_length=1, max_length=200, description="适用人群，如 成人外周血样本")
    direction: Literal["supportive", "contradictive"]
    detail: str = Field(default="", max_length=1000)


class DisputeOpen(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    intended_use: str = Field(min_length=2, max_length=2000)
    metric: str = Field(min_length=1, max_length=120)
    population: str = Field(min_length=1, max_length=200)
    evidence_ids: list[int] = Field(min_length=2, max_length=100, description="结论相互冲突的材料，至少两份")
    raised_by: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=2000)


class DisputeRestriction(BaseModel):
    """裁决施加的临时限制（受限场景）。"""

    scene: str = Field(min_length=1, max_length=200, description="受限场景，如 特定样本类型/区域/人群")
    limitation: str = Field(min_length=1, max_length=1000)


class EvidenceDisposition(BaseModel):
    evidence_id: int
    outcome: Literal["upheld", "rejected", "restricted"]
    note: str = Field(default="", max_length=1000)


class DisputeRulingRequest(BaseModel):
    arbiter: str = Field(min_length=1, max_length=120, description="无利益冲突的裁决人")
    comparison_basis: str = Field(min_length=10, max_length=4000, description="比较依据：样本量、方法学、适用条件等")
    applicable_scope: str = Field(min_length=2, max_length=2000, description="本裁决适用的用途、指标与人群范围")
    interim_restrictions: list[DisputeRestriction] = Field(default_factory=list, max_length=50)
    dispositions: list[EvidenceDisposition] = Field(min_length=1, max_length=100)
    based_on_version: int = Field(default=0, ge=0, description="修订时填写所基于的当前裁决版本；首次裁决为 0")
    rationale: str = Field(default="", max_length=4000)


class DisputeSupplement(BaseModel):
    evidence_id: int
    added_by: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    reopen: bool = Field(default=True, description="裁决后补充材料时是否重新开放争议以修订裁决")
