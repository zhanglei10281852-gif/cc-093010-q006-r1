from __future__ import annotations

from fastapi import APIRouter, Query

from app.disputes.schemas import DisputeOpen, DisputeRulingRequest, DisputeSupplement
from app.disputes.service import DisputeService


router = APIRouter(prefix="/api/disputes", tags=["证据争议与裁决"])


def service() -> DisputeService:
    return DisputeService()


@router.get("/conflicts")
def detect_conflicts(product_code: str | None = None):
    """自动识别同一产品、用途、指标和适用人群上的结论冲突。"""
    return {"items": service().detect_conflicts(product_code)}


@router.post("/disputes", status_code=201)
def open_dispute(payload: DisputeOpen):
    return service().open_dispute(payload.model_dump())


@router.get("/disputes")
def list_disputes(product_code: str | None = None, status: str | None = Query(default=None, pattern="^(open|ruled|superseded)$")):
    return {"items": service().list_disputes(product_code, status)}


@router.get("/disputes/{dispute_ref}")
def get_dispute(dispute_ref: str):
    return service().get_dispute(dispute_ref)


@router.post("/disputes/{dispute_id}/rulings")
def add_ruling(dispute_id: int, payload: DisputeRulingRequest):
    return service().add_ruling(dispute_id, payload.model_dump())


@router.post("/disputes/{dispute_id}/supplements", status_code=201)
def add_supplement(dispute_id: int, payload: DisputeSupplement):
    return service().add_supplement(dispute_id, payload.model_dump())


@router.get("/landscape/{product_code}")
def landscape(product_code: str):
    """目录查询：当前可用证据、未决冲突、受限场景与完整裁决历史。"""
    return service().landscape(product_code)
