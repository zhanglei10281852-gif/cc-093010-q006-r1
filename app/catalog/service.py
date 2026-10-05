from __future__ import annotations

import sqlite3

from app.catalog.repository import CatalogRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


class CatalogService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CatalogRepository(self.connection)

    def create_product(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.product_by_code(code):
            raise ConflictError("产品编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "organization": data["organization"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_product(payload, to_storage(self.clock.now()))

    def update_product(self, code: str, changes: dict) -> dict:
        product = self.repository.product_by_code(code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的产品字段")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).update_product(product["id"], values, to_storage(self.clock.now()))

    def list_products(self, category: str | None, status: str | None, active_only: bool, limit: int) -> list[dict]:
        return self.repository.list_products(category=category, status=status, active_only=active_only, limit=limit)

    def create_site(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.site_by_code(code):
            raise ConflictError("场地编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "region": data["region"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_site(payload, to_storage(self.clock.now()))

    def update_site(self, code: str, changes: dict) -> dict:
        site = self.repository.site_by_code(code)
        if site is None:
            raise NotFoundError("试点场地不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的场地字段")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).update_site(site["id"], values, to_storage(self.clock.now()))

    def list_sites(self, status: str | None, site_type: str | None, capability: str | None) -> list[dict]:
        return self.repository.list_sites(status=status, site_type=site_type, capability=capability)

    def submit_evidence(self, data: dict) -> dict:
        product = self.repository.product_by_code(data["product_code"])
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        duplicate = self.repository.evidence_duplicate(product["id"], data["evidence_type"], data["version"], data["content_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_evidence(product["id"], data, to_storage(self.clock.now()))

    def review_evidence(self, evidence_id: int, reviewer: str, decision: str, note: str) -> dict:
        evidence = self.repository.evidence_by_id(evidence_id)
        if evidence is None:
            raise NotFoundError("证据材料不存在")
        if evidence["status"] != "submitted":
            raise ConflictError("只有待审阅材料可以作出决定")
        if decision == "rejected" and len(note.strip()) < 4:
            raise ValidationError("驳回时需要说明可执行的原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CatalogRepository(connection)
            reviewed = repository.review_evidence(evidence_id, reviewer.strip(), decision, now)
            auto_dispute = None
            if decision == "accepted" and reviewed["claim_scope_key"]:
                auto_dispute = self._auto_freeze_conflict(repository, reviewed, reviewer.strip(), now)
            result = dict(reviewed)
            result["auto_dispute"] = auto_dispute
            return result

    def _auto_freeze_conflict(self, repository: CatalogRepository, evidence: dict, actor: str, now: str) -> dict | None:
        """接受后同一用途/指标/人群存在相反结论时，自动建立/重开争议并冻结材料。"""
        open_dispute = repository.open_dispute_by_scope(evidence["product_id"], evidence["claim_scope_key"])
        if open_dispute is not None:
            # 未决争议期间又接受了同范围材料：把它追加冻结进既有争议
            self._attach_if_new(repository, open_dispute["id"], [evidence], actor, now)
            return {"dispute_id": open_dispute["id"], "code": open_dispute["code"], "action": "attached",
                    "frozen_evidence_ids": [evidence["id"]]}
        conflicts = repository.accepted_claim_scopes_with_conflict(evidence["product_id"])
        if not any(item["claim_scope_key"] == evidence["claim_scope_key"] for item in conflicts):
            return None
        claims = [
            item for item in repository.claims_in_scope(evidence["product_id"], evidence["claim_scope_key"])
            if item["status"] == "accepted"
        ]
        latest = repository.latest_dispute_by_scope(evidence["product_id"], evidence["claim_scope_key"])
        if latest is not None and latest["status"] == "resolved":
            # 新的相反材料使冲突再次成立：重开原争议，保留完整裁决历史
            repository.reopen_dispute(latest["id"], f"新的相反结论材料 #{evidence['id']} 经审阅接受，自动重开", now)
            repository.supersede_current_ruling(latest["id"], now)
            self._attach_if_new(repository, latest["id"], claims, actor, now)
            return {"dispute_id": latest["id"], "code": latest["code"], "action": "reopened",
                    "frozen_evidence_ids": [item["id"] for item in claims]}
        sequence = int(repository.connection.execute(
            "SELECT COUNT(*) FROM evidence_disputes WHERE product_id=? AND scope_key=?",
            (evidence["product_id"], evidence["claim_scope_key"]),
        ).fetchone()[0]) + 1
        code = f"DSP-auto-{evidence['product_id']}-{evidence['claim_scope_key'][:8]}-{sequence}"
        dispute = repository.create_dispute(
            code=code, product_id=evidence["product_id"],
            use=evidence["claim_use"], metric=evidence["claim_metric"], population=evidence["claim_population"],
            scope_key=evidence["claim_scope_key"], opened_by=actor, dedupe_key=None, now=now,
        )
        self._attach_if_new(repository, dispute["id"], claims, actor, now)
        return {"dispute_id": dispute["id"], "code": dispute["code"], "action": "opened",
                "frozen_evidence_ids": [item["id"] for item in claims]}

    @staticmethod
    def _attach_if_new(repository: CatalogRepository, dispute_id: int, materials: list[dict], actor: str, now: str) -> None:
        for item in materials:
            if repository.material_prior_status(dispute_id, item["id"]) is None:
                repository.attach_material(dispute_id, item["id"], item["status"], actor, now)
            repository.set_evidence_status(item["id"], "disputed")

    def list_evidence(self, product_code: str | None, status: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.list_evidence(product_id=product_id, status=status)

    def submit_feedback(self, data: dict) -> dict:
        product = self.repository.product_by_code(data["product_code"])
        if product is None or not product["active"]:
            raise NotFoundError("可体验的健康创新产品不存在")
        site = self.repository.site_by_code(data["site_code"])
        if site is None or site["status"] != "active":
            raise NotFoundError("可用的试点场地不存在")
        duplicate = self.repository.feedback_duplicate(site["id"], data["session_reference"], data["audience_type"], data["contact_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_feedback(product["id"], site["id"], data, to_storage(self.clock.now()))

    def feedback_summary(self, product_code: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.feedback_summary(product_id)

