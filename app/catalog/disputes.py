from __future__ import annotations

import json
import sqlite3

from app.catalog.repository import CatalogRepository, claim_scope_digest
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


# 存在未决争议，或裁决未明确支持时，新的试点方案不得引用该产品
BLOCKING_RULING_DECISIONS = {"prefer_concern", "inconclusive", "restricted_use"}


class _ConcurrentDisputeError(Exception):
    """并发开启同一范围争议：唯一索引命中，需要折叠到既有争议。"""


class DisputeService:
    """证据争议的识别、冻结、利益冲突裁决、修订重开与目录总览。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CatalogRepository(self.connection)

    # ------------------------------------------------------------------
    # 争议开启与材料冻结
    # ------------------------------------------------------------------
    def open_dispute(self, payload: dict) -> dict:
        product = self.repository.product_by_code(payload["product_code"])
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        scope_key = claim_scope_digest(payload["use"], payload["metric"], payload["population"])
        now = to_storage(self.clock.now())
        try:
            with transaction(immediate=True) as connection:
                return self._open_dispute(
                    CatalogRepository(connection),
                    product=product,
                    use=payload["use"].strip(),
                    metric=payload["metric"].strip(),
                    population=payload["population"].strip(),
                    scope_key=scope_key,
                    evidence_ids=payload.get("evidence_ids"),
                    opened_by=payload["opened_by"].strip(),
                    dedupe_key=payload.get("dedupe_key"),
                    now=now,
                )
        except _ConcurrentDisputeError:
            # 并发开启同一范围争议：唯一索引只允许一条未决争议，折叠复用它
            existing = self.repository.open_dispute_by_scope(product["id"], scope_key)
            if existing is None and payload.get("dedupe_key"):
                existing = self.repository.dispute_by_dedupe_key(payload["dedupe_key"])
            if existing is None:
                raise ConflictError("争议开启与并发请求冲突，请重试")
            return self.get_dispute(existing["id"])

    def _open_dispute(
        self,
        repository: CatalogRepository,
        *,
        product: dict,
        use: str,
        metric: str,
        population: str,
        scope_key: str,
        evidence_ids: list[int] | None,
        opened_by: str,
        dedupe_key: str | None,
        now: str,
    ) -> dict:
        existing = repository.open_dispute_by_scope(product["id"], scope_key)
        if existing is not None:
            if evidence_ids:
                self._freeze_materials(repository, existing["id"], product["id"], scope_key, evidence_ids, opened_by, now)
            return self._detail(repository, existing["id"])
        if dedupe_key:
            deduped = repository.dispute_by_dedupe_key(dedupe_key)
            if deduped is not None and deduped["status"] == "open":
                return self._detail(repository, deduped["id"])

        if evidence_ids:
            materials = repository.evidence_by_ids(list(dict.fromkeys(evidence_ids)))
            if len(materials) != len(set(evidence_ids)):
                raise NotFoundError("部分证据材料不存在")
            self._validate_scope_materials(materials, product["id"], scope_key)
            sides = {str(item["claim_conclusion"]) for item in materials}
        else:
            materials = repository.claims_in_scope(product["id"], scope_key)
            sides = {str(item["claim_conclusion"]) for item in materials if item["status"] == "accepted"}
        if not {"supports", "concern"} <= sides:
            raise ValidationError("争议需要在同一用途、指标和适用人群下同时包含支持与担忧两类相反结论的材料")
        # 自动识别时只冻结双方已接受的材料；显式指定时冻结全部指定材料
        if not evidence_ids:
            materials = [item for item in materials if item["status"] == "accepted"]

        sequence = int(repository.connection.execute(
            "SELECT COUNT(*) FROM evidence_disputes WHERE product_id=? AND scope_key=?",
            (product["id"], scope_key),
        ).fetchone()[0]) + 1
        code = f"DSP-{product['code']}-{scope_key[:8]}-{sequence}"
        try:
            dispute = repository.create_dispute(
                code=code, product_id=product["id"], use=use, metric=metric, population=population,
                scope_key=scope_key, opened_by=opened_by, dedupe_key=dedupe_key, now=now,
            )
        except sqlite3.IntegrityError as exc:
            # 并发开启命中（产品，范围）未决唯一索引或幂等键唯一索引，交由外层折叠
            raise _ConcurrentDisputeError(str(exc)) from exc
        self._freeze_materials(repository, dispute["id"], product["id"], scope_key, [item["id"] for item in materials], opened_by, now)
        return self._detail(repository, dispute["id"])

    @staticmethod
    def _validate_scope_materials(materials: list[dict], product_id: int, scope_key: str) -> None:
        for item in materials:
            if item["product_id"] != product_id:
                raise ValidationError("证据材料不属于该产品")
            if item["claim_scope_key"] != scope_key:
                raise ValidationError("证据材料的用途、指标或适用人群与争议范围不一致")
            if item["claim_conclusion"] not in {"supports", "concern"}:
                raise ValidationError("只有携带结构化结论的证据材料可以进入争议")

    def _freeze_materials(
        self,
        repository: CatalogRepository,
        dispute_id: int,
        product_id: int,
        scope_key: str,
        evidence_ids: list[int],
        actor: str,
        now: str,
    ) -> None:
        ids = list(dict.fromkeys(evidence_ids))
        materials = repository.evidence_by_ids(ids)
        if len(materials) != len(set(ids)):
            raise NotFoundError("部分证据材料不存在")
        self._validate_scope_materials(materials, product_id, scope_key)
        for item in materials:
            other_open = [did for did in repository.open_dispute_ids_for_evidence(item["id"]) if did != dispute_id]
            if other_open:
                raise ConflictError(f"证据材料 {item['id']} 已被其他未决争议冻结")
            attached = repository.material_prior_status(dispute_id, item["id"])
            if attached is None:
                repository.attach_material(dispute_id, item["id"], item["status"], actor, now)
            repository.set_evidence_status(item["id"], "disputed")

    def add_material(self, dispute_id: int, payload: dict) -> dict:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CatalogRepository(connection)
            dispute = repository.dispute_by_id(dispute_id)
            if dispute is None:
                raise NotFoundError("证据争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("只有未决争议可以追加材料")
            self._freeze_materials(
                repository, dispute_id, dispute["product_id"], dispute["scope_key"],
                [payload["evidence_id"]], payload["actor"].strip(), now,
            )
            return self._detail(repository, dispute_id)

    # ------------------------------------------------------------------
    # 裁决与修订
    # ------------------------------------------------------------------
    def rule(self, dispute_id: int, payload: dict) -> dict:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CatalogRepository(connection)
            dispute = repository.dispute_by_id(dispute_id)
            if dispute is None:
                raise NotFoundError("证据争议不存在")
            adjudicator = payload["adjudicator"].strip()
            idempotency_key = payload.get("idempotency_key")
            if idempotency_key:
                existing = repository.ruling_by_idempotency(dispute_id, idempotency_key)
                if existing is not None:
                    return self._detail(repository, dispute_id)
            if dispute["status"] != "open":
                raise ConflictError("裁决只能记录在未决争议上；如有补充材料请先申请重开")
            materials = repository.material_rows(dispute_id)
            if not materials:
                raise ValidationError("争议尚未冻结任何证据材料")
            self._assert_independent(adjudicator, materials)

            revision_no = repository.next_ruling_revision(dispute_id)
            try:
                repository.insert_ruling(
                    dispute_id=dispute_id, revision_no=revision_no, adjudicator=adjudicator,
                    decision=payload["decision"], comparison_basis=payload["comparison_basis"].strip(),
                    applicable_scope=payload["applicable_scope"],
                    interim_restrictions=[str(item).strip() for item in payload["interim_restrictions"] if str(item).strip()],
                    is_final=True, idempotency_key=idempotency_key, now=now,
                )
            except sqlite3.IntegrityError as exc:
                # 未决期间至多一条当前结论（部分唯一索引）；并发裁决或重复幂等在此被拦截
                if idempotency_key:
                    existing = repository.ruling_by_idempotency(dispute_id, idempotency_key)
                    if existing is not None:
                        return self._detail(repository, dispute_id)
                raise ConflictError("并发裁决冲突：该争议已有有效结论，补充材料后请先申请重开再修订") from exc
            self._apply_resolution(repository, materials, payload["decision"])
            repository.mark_dispute_resolved(dispute_id, now)
            return self._detail(repository, dispute_id)

    @staticmethod
    def _assert_independent(adjudicator: str, materials: list[dict]) -> None:
        interested = set()
        for item in materials:
            if item.get("submitted_by"):
                interested.add(str(item["submitted_by"]).strip())
            if item.get("reviewed_by"):
                interested.add(str(item["reviewed_by"]).strip())
        if adjudicator in interested:
            raise ConflictError("裁决人与材料提交人或原审阅人存在利益关联，必须更换无利益冲突的审阅人")

    @staticmethod
    def _apply_resolution(repository: CatalogRepository, materials: list[dict], decision: str) -> None:
        for item in materials:
            prior = item.get("prior_status")
            if decision == "prefer_supports":
                target = "accepted" if item["claim_conclusion"] == "supports" else "rejected"
            elif decision == "prefer_concern":
                target = "accepted" if item["claim_conclusion"] == "concern" else "rejected"
            elif decision == "inconclusive":
                target = prior or "submitted"
            else:  # restricted_use：结论暂不单独采信，回到待审阅，由临时限制约束使用
                target = "submitted"
            repository.set_evidence_status(item["id"], target)

    def reopen(self, dispute_id: int, payload: dict) -> dict:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CatalogRepository(connection)
            dispute = repository.dispute_by_id(dispute_id)
            if dispute is None:
                raise NotFoundError("证据争议不存在")
            if dispute["status"] != "resolved":
                raise ConflictError("只有已裁决争议可以因补充材料重开")
            scope_occupied = repository.open_dispute_by_scope(dispute["product_id"], dispute["scope_key"])
            if scope_occupied is not None and scope_occupied["id"] != dispute_id:
                raise ConflictError("该范围已存在其他未决争议")
            repository.reopen_dispute(dispute_id, payload["reason"].strip(), now)
            repository.supersede_current_ruling(dispute_id, now)
            material_ids = [row["evidence_id"] for row in repository.material_rows(dispute_id)]
            self._freeze_materials(
                repository, dispute_id, dispute["product_id"], dispute["scope_key"],
                material_ids, payload["actor"].strip(), now,
            )
            if payload.get("evidence_ids"):
                self._freeze_materials(
                    repository, dispute_id, dispute["product_id"], dispute["scope_key"],
                    payload["evidence_ids"], payload["actor"].strip(), now,
                )
            return self._detail(repository, dispute_id)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_dispute(self, dispute_id: int) -> dict:
        return self._detail(self.repository, dispute_id)

    def list_disputes(self, product_code: str | None, status: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.list_disputes(product_id=product_id, status=status)

    def overview(self, product_code: str) -> dict:
        product = self.repository.product_by_code(product_code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        disputes = self.repository.list_disputes(product_id=product["id"], status=None)
        covered_evidence_ids: set[int] = set()
        for dispute_row in disputes:
            current = self.repository.current_ruling(dispute_row["id"])
            if current is not None and current["decision"] in BLOCKING_RULING_DECISIONS:
                covered_evidence_ids.update(
                    int(row["evidence_id"]) for row in self.repository.material_rows(dispute_row["id"])
                )
        usable: list[dict] = []
        for row in self.repository.list_evidence(product_id=product["id"], status=None):
            if row["status"] != "accepted":
                continue
            if row["id"] in covered_evidence_ids:
                continue  # 被阻断性裁决覆盖的材料不能继续支撑使用
            if row["claim_conclusion"] == "concern":
                continue  # 担忧类结论不作为支撑证据
            usable.append({
                "evidence_id": row["id"],
                "evidence_type": row["evidence_type"],
                "title": row["title"],
                "source_name": row["source_name"],
                "source_region": row["source_region"],
                "version": row["version"],
                "use": row["claim_use"],
                "metric": row["claim_metric"],
                "population": row["claim_population"],
                "reviewed_at": row["reviewed_at"],
            })
        pending: list[dict] = []
        restricted_scenarios: list[dict] = []
        histories: list[dict] = []
        for dispute in disputes:
            detail = self._detail(self.repository, dispute["id"])
            histories.append({
                "dispute_code": dispute["code"],
                "scope": {"use": dispute["claim_use"], "metric": dispute["claim_metric"], "population": dispute["claim_population"]},
                "status": dispute["status"],
                "rulings": detail["rulings"],
            })
            if dispute["status"] == "open":
                pending.append({
                    "dispute_code": dispute["code"],
                    "use": dispute["claim_use"],
                    "metric": dispute["claim_metric"],
                    "population": dispute["claim_population"],
                    "opened_at": dispute["opened_at"],
                    "materials": [
                        {"evidence_id": item["evidence_id"], "title": item["title"], "conclusion": item["conclusion"],
                         "source_region": item["source_region"], "prior_status": item.get("prior_status")}
                        for item in detail["materials"]
                    ],
                })
            current = next((item for item in detail["rulings"] if item["is_current"]), None)
            if current is not None and current["decision"] in BLOCKING_RULING_DECISIONS:
                restricted_scenarios.append({
                    "dispute_code": dispute["code"],
                    "decision": current["decision"],
                    "use": dispute["claim_use"],
                    "metric": dispute["claim_metric"],
                    "population": dispute["claim_population"],
                    "applicable_scope": current["applicable_scope"],
                    "interim_restrictions": current["interim_restrictions"],
                    "adjudicator": current["adjudicator"],
                    "ruled_at": current["created_at"],
                })
        return {
            "product_code": product["code"],
            "product_name": product["name"],
            "usable_evidence": usable,
            "pending_conflicts": pending,
            "restricted_scenarios": restricted_scenarios,
            "new_pilot_blocked": bool(pending) or bool(restricted_scenarios),
            "ruling_history": histories,
        }

    # ------------------------------------------------------------------
    # 试点联动
    # ------------------------------------------------------------------
    def product_evidence_gate(self, product_id: int) -> dict | None:
        """返回产品当前的证据阻断原因；无阻断返回 None。"""
        disputes = self.repository.list_disputes(product_id=product_id, status=None)
        for dispute in disputes:
            if dispute["status"] == "open":
                return {"reason": "存在未决证据争议", "dispute_code": dispute["code"]}
            current = self.repository.current_ruling(dispute["id"])
            if current is not None and current["decision"] in BLOCKING_RULING_DECISIONS:
                return {
                    "reason": "证据裁决限制该产品用于新的试点方案",
                    "dispute_code": dispute["code"],
                    "decision": current["decision"],
                    "interim_restrictions": json.loads(current["interim_restrictions_json"]),
                }
        return None

    def session_evidence_snapshot(self, product_id: int) -> dict:
        disputes = self.repository.list_disputes(product_id=product_id, status=None)
        open_codes: list[str] = []
        current_rulings: list[dict] = []
        for dispute in disputes:
            if dispute["status"] == "open":
                open_codes.append(dispute["code"])
            ruling = self.repository.current_ruling(dispute["id"])
            if ruling is not None:
                current_rulings.append({
                    "dispute_code": dispute["code"],
                    "decision": ruling["decision"],
                    "interim_restrictions": json.loads(ruling["interim_restrictions_json"]),
                })
        return {"captured_at": to_storage(self.clock.now()), "open_disputes": open_codes, "current_rulings": current_rulings}

    # ------------------------------------------------------------------
    def _detail(self, repository: CatalogRepository, dispute_id: int) -> dict:
        dispute = repository.dispute_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError("证据争议不存在")
        product = repository.product_by_id(dispute["product_id"]) or {}
        materials = repository.material_rows(dispute_id)
        rulings: list[dict] = []
        for row in repository.ruling_history(dispute_id):
            rulings.append({
                "revision_no": row["revision_no"],
                "adjudicator": row["adjudicator"],
                "decision": row["decision"],
                "comparison_basis": row["comparison_basis"],
                "applicable_scope": json.loads(row["applicable_scope_json"]),
                "interim_restrictions": json.loads(row["interim_restrictions_json"]),
                "is_current": bool(row["is_current"]),
                "created_at": row["created_at"],
                "superseded_at": row["superseded_at"],
            })
        return {
            "id": dispute["id"],
            "code": dispute["code"],
            "product_code": product.get("code"),
            "product_name": product.get("name"),
            "scope": {"use": dispute["claim_use"], "metric": dispute["claim_metric"], "population": dispute["claim_population"]},
            "status": dispute["status"],
            "opened_by": dispute["opened_by"],
            "opened_at": dispute["opened_at"],
            "resolved_at": dispute["resolved_at"],
            "reopen_reason": dispute["reopen_reason"],
            "materials": [
                {
                    "evidence_id": item["id"],
                    "title": item["title"],
                    "evidence_type": item["evidence_type"],
                    "source_name": item["source_name"],
                    "source_region": item["source_region"],
                    "version": item["version"],
                    "conclusion": item["claim_conclusion"],
                    "frozen_status": item["status"],
                    "prior_status": item.get("prior_status"),
                    "submitted_by": item["submitted_by"],
                    "reviewed_by": item["reviewed_by"],
                }
                for item in materials
            ],
            "current_ruling": next((item for item in rulings if item["is_current"]), None),
            "rulings": rulings,
        }
