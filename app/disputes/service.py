from __future__ import annotations

import json
import sqlite3

from app.disputes.findings import (
    CONTRADICTIVE,
    SUPPORTIVE,
    conflict_groups,
    scope_digest,
    scopes_of_document,
)
from app.disputes.repository import DisputeRepository
from app.catalog.repository import CatalogRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


class DisputeService:
    """证据争议识别、冻结、无利益冲突裁决、修订与目录聚合。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = DisputeRepository(self.connection)

    # ------------------------------------------------------------------ 冲突识别

    def detect_conflicts(self, product_code: str | None = None) -> list[dict]:
        """自动扫描同一产品、用途、指标、人群下的相反结论。"""
        if product_code:
            product = CatalogRepository(self.connection).product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            documents = self.repository.evidence_for_product(int(product["id"]))
        else:
            documents = [dict(row) for row in self.connection.execute(
                "SELECT * FROM evidence_documents ORDER BY id"
            ).fetchall()]
        groups = conflict_groups(documents)
        result: list[dict] = []
        for digest, group in sorted(groups.items(), key=lambda item: (item[1]["product_id"], item[0])):
            active = self.repository.active_dispute_for_scope(group["product_id"], digest)
            evidence = [self._evidence_brief(self.repository.evidence_by_id(eid)) for eid in sorted(group["evidence_ids"])]
            result.append({
                "product_id": group["product_id"],
                "intended_use": group["intended_use"],
                "metric": group["metric"],
                "population": group["population"],
                "evidence": evidence,
                "active_dispute_code": active["code"] if active else None,
                "already_frozen": not group["has_unfrozen"],
            })
        return result

    # ------------------------------------------------------------------ 开争议

    def open_dispute(self, payload: dict) -> dict:
        product = CatalogRepository(self.connection).product_by_code(payload["product_code"])
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        product_id = int(product["id"])
        digest = scope_digest(product_id, payload["intended_use"], payload["metric"], payload["population"])
        documents = self.repository.evidence_for_product(product_id)
        groups = conflict_groups(documents, include_settled=True)
        group = groups.get(digest)
        if group is None:
            raise ValidationError("该适用范围下不存在结论方向相反的材料对，不能开立争议")
        requested = set(payload["evidence_ids"])
        covered = group["evidence_ids"]
        missing = requested - covered
        if missing:
            raise ValidationError("所列材料不属于该产品在此用途、指标、人群下的冲突范围", context={"evidence_ids": sorted(missing)})
        existing = self.repository.active_dispute_for_scope(product_id, digest)
        if existing is not None:
            raise ConflictError(
                "该适用范围已存在有效争议，重复检测不得生成第二条争议",
                context={"dispute_id": existing["id"], "dispute_code": existing["code"], "status": existing["status"]},
            )
        already_frozen = {
            int(document["id"]): int(document["dispute_id"])
            for document in documents
            if int(document["id"]) in covered and document.get("dispute_id") is not None
        }
        if already_frozen:
            raise ConflictError(
                "相关材料已冻结在另一争议中，不能重复开立争议",
                context={"evidence_dispute_ids": already_frozen},
            )
        directions = {
            direction
            for eid in requested
            for scope in self._document_scopes(self.repository.evidence_by_id(eid), digest)
            for direction in scope["directions"]
        }
        if SUPPORTIVE not in directions or CONTRADICTIVE not in directions:
            raise ValidationError("争议必须同时覆盖支持与反对结论的材料")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DisputeRepository(connection)
            if repository.active_dispute_for_scope(product_id, digest) is not None:
                raise ConflictError("该适用范围已存在有效争议，重复检测不得生成第二条争议")
            code = f"DSP-{product['code']}-{digest[:8]}"
            try:
                dispute = repository.create_dispute(
                    code=code, product_id=product_id,
                    intended_use=payload["intended_use"].strip(),
                    metric=payload["metric"].strip(),
                    population=payload["population"].strip(),
                    raised_by=payload["raised_by"].strip(), now=now,
                )
            except sqlite3.IntegrityError as exc:  # 并发重复检测：同一范围至多一条有效争议
                raise ConflictError("该适用范围已存在有效争议，重复检测不得生成第二条争议") from exc
            # 冻结该范围下全部相关材料（含调用方未逐一列出的），避免目录同时展示为已接受
            repository.freeze_evidence(sorted(covered), int(dispute["id"]))
            return self._detail(connection, dispute)

    # ------------------------------------------------------------------ 裁决

    def add_ruling(self, dispute_id: int, payload: dict) -> dict:
        dispute = self.repository.dispute_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError("证据争议不存在")
        if dispute["status"] == "superseded":
            raise ConflictError("争议已被并入新争议，不能继续裁决")
        covered = self.repository.frozen_evidence(dispute_id)
        covered_ids = {int(item["id"]) for item in covered}
        dispositions = payload["dispositions"]
        disposition_ids = {int(item["evidence_id"]) for item in dispositions}
        unknown = disposition_ids - covered_ids
        if unknown:
            raise ValidationError("裁决处置对象不属于本争议覆盖材料", context={"evidence_ids": sorted(unknown)})
        if disposition_ids != covered_ids:
            raise ValidationError("裁决必须对争议覆盖的每份材料给出处置结论", context={"missing_evidence_ids": sorted(covered_ids - disposition_ids)})
        if len(disposition_ids) != len(dispositions):
            raise ValidationError("同一材料在裁决中出现了多条处置")
        arbiter = payload["arbiter"].strip()
        interested = {item["submitted_by"] for item in covered}
        if arbiter in interested:
            raise ConflictError("裁决人与争议材料存在利益冲突，必须由无利益冲突的审阅人裁决", context={"submitted_by": sorted(interested)})
        current_version = self.repository.latest_ruling_version(dispute_id)
        if int(payload["based_on_version"]) != current_version:
            raise ConflictError(
                "裁决所基于的版本已变化，请按当前裁决版本修订",
                context={"current_version": current_version, "submitted_based_on": payload["based_on_version"]},
            )
        restrictions = payload["interim_restrictions"]
        disposition_rows = [
            {"evidence_id": int(item["evidence_id"]), "outcome": item["outcome"], "note": item.get("note", "")}
            for item in dispositions
        ]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DisputeRepository(connection)
            try:
                ruling = repository.add_ruling(
                    dispute_id=dispute_id, version=current_version + 1,
                    arbiter=arbiter,
                    comparison_basis=payload["comparison_basis"].strip(),
                    applicable_scope=payload["applicable_scope"].strip(),
                    interim_restrictions=restrictions,
                    basis_evidence_ids=sorted(covered_ids),
                    dispositions=disposition_rows,
                    based_on_version=current_version,
                    rationale=payload.get("rationale", "").strip(),
                    now=now,
                )
            except sqlite3.IntegrityError as exc:  # 并发裁决：同一版本只能写入一条
                raise ConflictError("并发裁决只允许生成一条有效结论，请基于最新版本修订") from exc
            repository.apply_disposition_statuses(dispute_id, {row["evidence_id"]: row["outcome"] for row in disposition_rows})
            return self._detail(connection, repository.dispute_by_id(dispute_id))

    # ------------------------------------------------------------------ 补充材料

    def add_supplement(self, dispute_id: int, payload: dict) -> dict:
        dispute = self.repository.dispute_by_id(dispute_id)
        if dispute is None:
            raise NotFoundError("证据争议不存在")
        evidence = self.repository.evidence_by_id(int(payload["evidence_id"]))
        if evidence is None:
            raise NotFoundError("证据材料不存在")
        if int(evidence["product_id"]) != int(dispute["product_id"]):
            raise ValidationError("补充材料与争议不属于同一产品")
        if evidence["dispute_id"] is not None and int(evidence["dispute_id"]) != dispute_id:
            raise ConflictError("材料已冻结在其他争议中")
        if not self._document_scopes(evidence, dispute["scope_digest"]):
            raise ValidationError("补充材料未涉及争议的用途、指标和适用人群，不能并入该争议")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = DisputeRepository(connection)
            existing = connection.execute(
                "SELECT id FROM dispute_supplements WHERE dispute_id=? AND evidence_id=?",
                (dispute_id, evidence["id"]),
            ).fetchone()
            if existing is None:
                repository.add_supplement(dispute_id, int(evidence["id"]), payload["added_by"].strip(), payload["note"].strip(), now)
                connection.execute(
                    "UPDATE evidence_documents SET status='disputed',dispute_id=? WHERE id=? AND dispute_id IS NULL",
                    (dispute_id, evidence["id"]),
                )
            if payload.get("reopen", True) and dispute["status"] == "ruled":
                # 裁决后补充材料：争议重新开放，裁决历史保留，等待修订
                repository.reopen_dispute(dispute_id, now)
            return self._detail(connection, repository.dispute_by_id(dispute_id))

    # ------------------------------------------------------------------ 查询

    def list_disputes(self, product_code: str | None, status: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = CatalogRepository(self.connection).product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = int(product["id"])
        items = self.repository.list_disputes(product_id=product_id, status=status)
        for item in items:
            ruling = self.repository.current_ruling(int(item["id"]))
            item["current_ruling_version"] = int(ruling["version"]) if ruling else None
        return items

    def get_dispute(self, dispute_code_or_id: str) -> dict:
        dispute = self._load_dispute(dispute_code_or_id)
        return self._detail(self.connection, dispute)

    def landscape(self, product_code: str) -> dict:
        """目录查询：当前可用证据、未决冲突、受限场景与完整裁决历史一次给出。"""
        product = CatalogRepository(self.connection).product_by_code(product_code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        product_id = int(product["id"])
        disputes = self.repository.active_disputes_for_product(product_id)
        open_disputes = [item for item in disputes if item["status"] == "open"]
        available = [
            self._evidence_brief(dict(row))
            for row in self.connection.execute(
                "SELECT * FROM evidence_documents WHERE product_id=? AND status='accepted' ORDER BY evidence_type,id",
                (product_id,),
            ).fetchall()
        ]
        restricted_scenes: list[dict] = []
        ruling_history: list[dict] = []
        for dispute in disputes:
            for ruling in self.repository.rulings(int(dispute["id"])):
                entry = self._ruling_brief(dispute, ruling)
                ruling_history.append(entry)
            current = self.repository.current_ruling(int(dispute["id"]))
            if current is not None:
                for restriction in json.loads(current["interim_restrictions_json"]):
                    restricted_scenes.append({
                        "dispute_code": dispute["code"],
                        "ruling_version": current["version"],
                        "scene": restriction.get("scene", ""),
                        "limitation": restriction.get("limitation", ""),
                    })
        return {
            "product_code": product["code"],
            "product_name": product["name"],
            "available_evidence": available,
            "open_disputes": [
                {
                    "dispute_code": item["code"],
                    "intended_use": item["intended_use"],
                    "metric": item["metric"],
                    "population": item["population"],
                    "raised_by": item["raised_by"],
                    "raised_at": item["raised_at"],
                    "frozen_evidence_ids": [row["id"] for row in self.repository.frozen_evidence(int(item["id"]))],
                }
                for item in open_disputes
            ],
            "detected_conflicts": self.detect_conflicts(product_code),
            "restricted_scenes": restricted_scenes,
            "ruling_history": ruling_history,
        }

    def evidence_gate(self, product_code: str, parameters: dict) -> dict:
        """新试点场次提交时的证据门禁结果与证据快照。"""
        product = CatalogRepository(self.connection).product_by_code(product_code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        landscape = self.landscape(product_code)
        if landscape["open_disputes"]:
            raise ConflictError(
                "产品存在未决证据争议，被争议覆盖的材料不能支撑新的试点场次",
                context={"dispute_codes": [item["dispute_code"] for item in landscape["open_disputes"]]},
            )
        blocked = [scene for scene in landscape["restricted_scenes"] if self._scene_matches(scene["scene"], parameters)]
        if blocked:
            raise ConflictError(
                "试点参数命中裁决限定的受限场景",
                context={"restricted_scenes": blocked},
            )
        disputes = self.repository.active_disputes_for_product(int(product["id"]))
        snapshot_disputes = []
        for dispute in disputes:
            current = self.repository.current_ruling(int(dispute["id"]))
            snapshot_disputes.append({
                "dispute_code": dispute["code"],
                "status": dispute["status"],
                "ruling_version": int(current["version"]) if current else None,
            })
        return {
            "snapshot_at": to_storage(self.clock.now()),
            "available_evidence": landscape["available_evidence"],
            "restricted_scenes": landscape["restricted_scenes"],
            "disputes": snapshot_disputes,
        }

    # ------------------------------------------------------------------ 内部工具

    def _load_dispute(self, dispute_code_or_id: str) -> dict:
        if str(dispute_code_or_id).isdigit():
            dispute = self.repository.dispute_by_id(int(dispute_code_or_id))
        else:
            dispute = self.repository.dispute_by_code(str(dispute_code_or_id))
        if dispute is None:
            raise NotFoundError("证据争议不存在")
        return dispute

    def _detail(self, connection: sqlite3.Connection, dispute: dict) -> dict:
        repository = DisputeRepository(connection)
        dispute_id = int(dispute["id"])
        result = dict(dispute)
        result["frozen_evidence"] = [self._evidence_brief(item) for item in repository.frozen_evidence(dispute_id)]
        result["supplements"] = repository.supplements(dispute_id)
        rulings = repository.rulings(dispute_id)
        result["rulings"] = [self._ruling_brief(dispute, ruling) for ruling in rulings]
        current = repository.current_ruling(dispute_id)
        result["current_ruling_version"] = int(current["version"]) if current else None
        return result

    @staticmethod
    def _evidence_brief(document: dict | None) -> dict | None:
        if document is None:
            return None
        return {
            "id": document["id"],
            "evidence_type": document["evidence_type"],
            "title": document["title"],
            "source_name": document["source_name"],
            "source_region": document["source_region"],
            "version": document["version"],
            "status": document["status"],
            "submitted_by": document["submitted_by"],
            "dispute_id": document.get("dispute_id"),
        }

    @staticmethod
    def _ruling_brief(dispute: dict, ruling: dict) -> dict:
        return {
            "dispute_code": dispute["code"],
            "version": ruling["version"],
            "arbiter": ruling["arbiter"],
            "comparison_basis": ruling["comparison_basis"],
            "applicable_scope": ruling["applicable_scope"],
            "interim_restrictions": json.loads(ruling["interim_restrictions_json"]),
            "dispositions": json.loads(ruling["dispositions_json"]),
            "based_on_version": ruling["based_on_version"],
            "rationale": ruling["rationale"],
            "created_at": ruling["created_at"],
        }

    @staticmethod
    def _document_scopes(document: dict | None, digest: str):
        if document is None:
            return []
        scopes = scopes_of_document(document)
        scope = scopes.get(digest)
        return [scope] if scope else []

    @staticmethod
    def _scene_matches(scene: str, parameters: dict) -> bool:
        target = scene.strip().lower()
        if not target:
            return False
        values = [str(value).strip().lower() for value in parameters.values() if isinstance(value, (str, int, float))]
        return target in values
