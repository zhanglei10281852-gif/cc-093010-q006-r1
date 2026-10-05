from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ProductCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    organization: str = Field(min_length=2, max_length=160)
    origin_country: str = Field(min_length=2, max_length=80)
    category: Literal["康复设备", "辅助诊断", "数字疗法", "慢病管理", "数字中医", "健康消费"]
    intended_use: str = Field(min_length=10, max_length=2000)
    risk_level: Literal["low", "medium", "high"]
    regulatory_status: Literal["展示", "研究", "已注册", "暂停"] = "展示"


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    intended_use: str | None = Field(default=None, min_length=10, max_length=2000)
    risk_level: Literal["low", "medium", "high"] | None = None
    regulatory_status: Literal["展示", "研究", "已注册", "暂停"] | None = None
    active: bool | None = None


class SiteCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    site_type: Literal["展会体验点", "医院", "康复机构", "研究机构", "产业伙伴"]
    region: str = Field(min_length=2, max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    max_concurrent: int = Field(default=1, ge=1, le=10000)


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    region: str | None = Field(default=None, min_length=2, max_length=120)
    capabilities: list[str] | None = Field(default=None, max_length=100)
    max_concurrent: int | None = Field(default=None, ge=1, le=10000)
    status: Literal["active", "suspended", "closed"] | None = None


class EvidenceClaim(BaseModel):
    use: str = Field(min_length=2, max_length=300, description="预期用途/使用场景")
    metric: str = Field(min_length=1, max_length=160, description="评价指标，如灵敏度、误报率")
    population: str = Field(min_length=2, max_length=300, description="适用人群/样本类型")
    conclusion: Literal["supports", "concern"]


class EvidenceSubmit(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    evidence_type: Literal["临床", "性能", "安全", "合规", "体验"]
    title: str = Field(min_length=2, max_length=200)
    source_name: str = Field(min_length=2, max_length=160)
    source_region: str = Field(min_length=2, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    content_digest: str = Field(min_length=16, max_length=128)
    summary: dict = Field(default_factory=dict)
    submitted_by: str = Field(min_length=1, max_length=120)
    claim: EvidenceClaim | None = None


class EvidenceReview(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    decision: Literal["accepted", "rejected"]
    note: str = Field(default="", max_length=2000)


class DisputeOpen(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    use: str = Field(min_length=2, max_length=300)
    metric: str = Field(min_length=1, max_length=160)
    population: str = Field(min_length=2, max_length=300)
    evidence_ids: list[int] | None = Field(default=None, max_length=200)
    opened_by: str = Field(min_length=1, max_length=120)
    dedupe_key: str | None = Field(default=None, max_length=160)


class DisputeAddMaterial(BaseModel):
    evidence_id: int
    actor: str = Field(min_length=1, max_length=120)


class DisputeRuling(BaseModel):
    adjudicator: str = Field(min_length=1, max_length=120)
    decision: Literal["prefer_supports", "prefer_concern", "inconclusive", "restricted_use"]
    comparison_basis: str = Field(min_length=10, max_length=4000, description="比较依据：样本量、方法学、地区差异等")
    applicable_scope: dict = Field(default_factory=dict, description="裁决适用范围，如地区、样本类型")
    interim_restrictions: list[str] = Field(default_factory=list, max_length=50, description="临时使用限制")
    idempotency_key: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def require_restrictions_for_restricted_use(self) -> "DisputeRuling":
        if self.decision == "restricted_use" and not any(item.strip() for item in self.interim_restrictions):
            raise ValueError("裁决为受限使用时必须记录至少一条临时限制")
        return self


class DisputeReopen(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=5, max_length=2000, description="补充材料或重开原因")
    evidence_ids: list[int] | None = Field(default=None, max_length=200)


class FeedbackSubmit(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    site_code: str = Field(min_length=2, max_length=64)
    session_reference: str = Field(min_length=2, max_length=120)
    audience_type: Literal["公众", "临床人员", "采购商", "产业伙伴"]
    rating: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=30)
    comment: str = Field(default="", max_length=2000)
    contact_digest: str = Field(default="", max_length=128)
    consent_to_follow_up: bool = False

    @model_validator(mode="after")
    def require_contact_for_follow_up(self) -> "FeedbackSubmit":
        if self.consent_to_follow_up and not self.contact_digest:
            raise ValueError("允许后续联系时必须提供联系人摘要")
        return self

