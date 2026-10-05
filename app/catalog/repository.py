from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def claim_scope_digest(use: str, metric: str, population: str) -> str:
    """同一产品、用途、指标、适用人群归一化为相同范围键。"""
    payload = {"use": use.strip(), "metric": metric.strip().lower(), "population": population.strip()}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class CatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def product_by_code(self, code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM health_products WHERE code=?", (code,)).fetchone())

    def product_by_id(self, product_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM health_products WHERE id=?", (product_id,)).fetchone())

    def create_product(self, data: dict, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO health_products(code,name,organization,origin_country,category,intended_use,risk_level,regulatory_status,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (data["code"], data["name"], data["organization"], data["origin_country"], data["category"], data["intended_use"], data["risk_level"], data["regulatory_status"], now, now),
        )
        return self.product_by_id(int(cursor.lastrowid)) or {}

    def update_product(self, product_id: int, changes: dict, now: str) -> dict[str, Any]:
        values = {key: value for key, value in changes.items() if value is not None}
        if "active" in values:
            values["active"] = 1 if values["active"] else 0
        assignments = [f"{key}=?" for key in values]
        self.connection.execute(
            f"UPDATE health_products SET {','.join(assignments)},updated_at=? WHERE id=?",
            (*values.values(), now, product_id),
        )
        return self.product_by_id(product_id) or {}

    def list_products(self, *, category: str | None, status: str | None, active_only: bool, limit: int) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("p.category=?")
            params.append(category)
        if status:
            clauses.append("p.regulatory_status=?")
            params.append(status)
        if active_only:
            clauses.append("p.active=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            "SELECT p.*,COUNT(DISTINCT e.id) AS evidence_count,COUNT(DISTINCT f.id) AS feedback_count "
            "FROM health_products p LEFT JOIN evidence_documents e ON e.product_id=p.id "
            "LEFT JOIN public_feedback f ON f.product_id=p.id" + where +
            " GROUP BY p.id ORDER BY p.updated_at DESC,p.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def site_by_code(self, code: str) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM pilot_sites WHERE code=?", (code,)).fetchone())

    def site_by_id(self, site_id: int) -> dict[str, Any] | None:
        return _dict(self.connection.execute("SELECT * FROM pilot_sites WHERE id=?", (site_id,)).fetchone())

    def create_site(self, data: dict, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO pilot_sites(code,name,site_type,region,capabilities_json,max_concurrent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'active',?,?)",
            (data["code"], data["name"], data["site_type"], data["region"], json.dumps(sorted(set(data["capabilities"])), ensure_ascii=False), data["max_concurrent"], now, now),
        )
        return self.site_by_id(int(cursor.lastrowid)) or {}

    def update_site(self, site_id: int, changes: dict, now: str) -> dict[str, Any]:
        values = {key: value for key, value in changes.items() if value is not None}
        if "capabilities" in values:
            values["capabilities_json"] = json.dumps(sorted(set(values.pop("capabilities"))), ensure_ascii=False)
        assignments = [f"{key}=?" for key in values]
        self.connection.execute(
            f"UPDATE pilot_sites SET {','.join(assignments)},updated_at=? WHERE id=?",
            (*values.values(), now, site_id),
        )
        return self.site_by_id(site_id) or {}

    def list_sites(self, *, status: str | None, site_type: str | None, capability: str | None) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if site_type:
            clauses.append("site_type=?")
            params.append(site_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = [dict(row) for row in self.connection.execute("SELECT * FROM pilot_sites" + where + " ORDER BY region,name", tuple(params)).fetchall()]
        if capability:
            rows = [row for row in rows if capability in json.loads(row["capabilities_json"])]
        return rows

    def evidence_duplicate(self, product_id: int, evidence_type: str, version: str, digest: str) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_documents WHERE product_id=? AND evidence_type=? AND version=? AND content_digest=?",
            (product_id, evidence_type, version, digest),
        ).fetchone())

    def evidence_by_id(self, evidence_id: int) -> dict | None:
        return _dict(self.connection.execute("SELECT * FROM evidence_documents WHERE id=?", (evidence_id,)).fetchone())

    def create_evidence(self, product_id: int, data: dict, now: str) -> dict:
        claim = data.get("claim")
        if claim:
            claim_use = claim["use"]
            claim_metric = claim["metric"]
            claim_population = claim["population"]
            claim_conclusion = claim["conclusion"]
            claim_scope_key = claim_scope_digest(claim_use, claim_metric, claim_population)
        else:
            claim_use = claim_metric = claim_population = claim_conclusion = claim_scope_key = None
        cursor = self.connection.execute(
            "INSERT INTO evidence_documents(product_id,evidence_type,title,source_name,source_region,version,content_digest,summary_json,status,submitted_by,submitted_at,claim_use,claim_metric,claim_population,claim_conclusion,claim_scope_key) VALUES(?,?,?,?,?,?,?,?, 'submitted',?,?,?,?,?,?,?)",
            (product_id, data["evidence_type"], data["title"], data["source_name"], data["source_region"], data["version"], data["content_digest"], json.dumps(data["summary"], ensure_ascii=False, sort_keys=True), data["submitted_by"], now, claim_use, claim_metric, claim_population, claim_conclusion, claim_scope_key),
        )
        return self.evidence_by_id(int(cursor.lastrowid)) or {}

    def review_evidence(self, evidence_id: int, reviewer: str, decision: str, now: str) -> dict:
        self.connection.execute(
            "UPDATE evidence_documents SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?",
            (decision, reviewer, now, evidence_id),
        )
        return self.evidence_by_id(evidence_id) or {}

    def list_evidence(self, *, product_id: int | None, status: str | None) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if product_id is not None:
            clauses.append("e.product_id=?")
            params.append(product_id)
        if status:
            clauses.append("e.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.connection.execute(
            "SELECT e.*,p.code AS product_code,p.name AS product_name FROM evidence_documents e JOIN health_products p ON p.id=e.product_id" + where + " ORDER BY e.submitted_at DESC,e.id DESC",
            tuple(params),
        ).fetchall()]

    def feedback_duplicate(self, site_id: int, session_reference: str, audience_type: str, contact_digest: str) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM public_feedback WHERE site_id=? AND session_reference=? AND audience_type=? AND contact_digest=?",
            (site_id, session_reference, audience_type, contact_digest),
        ).fetchone())

    def create_feedback(self, product_id: int, site_id: int, data: dict, now: str) -> dict:
        cursor = self.connection.execute(
            "INSERT INTO public_feedback(product_id,site_id,session_reference,audience_type,rating,tags_json,comment,contact_digest,consent_to_follow_up,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (product_id, site_id, data["session_reference"], data["audience_type"], data["rating"], json.dumps(sorted(set(data["tags"])), ensure_ascii=False), data["comment"], data["contact_digest"], 1 if data["consent_to_follow_up"] else 0, now),
        )
        return dict(self.connection.execute("SELECT * FROM public_feedback WHERE id=?", (cursor.lastrowid,)).fetchone())

    def feedback_summary(self, product_id: int | None = None) -> list[dict]:
        where = " WHERE p.id=?" if product_id is not None else ""
        params = (product_id,) if product_id is not None else ()
        rows = self.connection.execute(
            "SELECT p.id AS product_id,p.code AS product_code,p.name AS product_name,COUNT(f.id) AS feedback_count,"
            "ROUND(AVG(f.rating),2) AS average_rating,SUM(CASE WHEN f.consent_to_follow_up=1 THEN 1 ELSE 0 END) AS follow_up_count "
            "FROM health_products p LEFT JOIN public_feedback f ON f.product_id=p.id" + where + " GROUP BY p.id ORDER BY feedback_count DESC,p.code",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 证据争议与裁决
    # ------------------------------------------------------------------
    def evidence_by_ids(self, evidence_ids: list[int]) -> list[dict]:
        if not evidence_ids:
            return []
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self.connection.execute(
            f"SELECT * FROM evidence_documents WHERE id IN ({placeholders})",
            tuple(evidence_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_evidence_status(self, evidence_id: int, status: str) -> None:
        self.connection.execute("UPDATE evidence_documents SET status=? WHERE id=?", (status, evidence_id))

    def claims_in_scope(self, product_id: int, scope_key: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_documents WHERE product_id=? AND claim_scope_key=? "
            "AND claim_conclusion IS NOT NULL ORDER BY id",
            (product_id, scope_key),
        ).fetchall()
        return [dict(row) for row in rows]

    def accepted_claim_scopes_with_conflict(self, product_id: int) -> list[dict]:
        """返回同一范围内同时存在 supports 与 concern 且均已接受的范围。"""
        rows = self.connection.execute(
            "SELECT claim_scope_key,claim_use,claim_metric,claim_population,"
            "GROUP_CONCAT(DISTINCT claim_conclusion) AS conclusions,"
            "COUNT(*) AS amount FROM evidence_documents "
            "WHERE product_id=? AND status='accepted' AND claim_scope_key IS NOT NULL "
            "GROUP BY claim_scope_key HAVING SUM(CASE WHEN claim_conclusion='supports' THEN 1 ELSE 0 END)>0 "
            "AND SUM(CASE WHEN claim_conclusion='concern' THEN 1 ELSE 0 END)>0",
            (product_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def open_dispute_by_scope(self, product_id: int, scope_key: str) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_disputes WHERE product_id=? AND scope_key=? AND status='open'",
            (product_id, scope_key),
        ).fetchone())

    def latest_dispute_by_scope(self, product_id: int, scope_key: str) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_disputes WHERE product_id=? AND scope_key=? ORDER BY id DESC LIMIT 1",
            (product_id, scope_key),
        ).fetchone())

    def dispute_by_id(self, dispute_id: int) -> dict | None:
        return _dict(self.connection.execute("SELECT * FROM evidence_disputes WHERE id=?", (dispute_id,)).fetchone())

    def dispute_by_code(self, code: str) -> dict | None:
        return _dict(self.connection.execute("SELECT * FROM evidence_disputes WHERE code=?", (code,)).fetchone())

    def dispute_by_dedupe_key(self, dedupe_key: str) -> dict | None:
        return _dict(self.connection.execute("SELECT * FROM evidence_disputes WHERE dedupe_key=?", (dedupe_key,)).fetchone())

    def create_dispute(self, *, code: str, product_id: int, use: str, metric: str, population: str, scope_key: str, opened_by: str, dedupe_key: str | None, now: str) -> dict:
        cursor = self.connection.execute(
            "INSERT INTO evidence_disputes(code,product_id,claim_use,claim_metric,claim_population,scope_key,status,opened_by,dedupe_key,opened_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?, 'open',?,?,?,?,?)",
            (code, product_id, use, metric, population, scope_key, opened_by, dedupe_key, now, now, now),
        )
        return self.dispute_by_id(int(cursor.lastrowid)) or {}

    def attach_material(self, dispute_id: int, evidence_id: int, prior_status: str | None, actor: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO evidence_dispute_materials(dispute_id,evidence_id,prior_status,added_by,added_at) VALUES(?,?,?,?,?)",
            (dispute_id, evidence_id, prior_status, actor, now),
        )

    def material_rows(self, dispute_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT m.evidence_id,m.prior_status,m.added_by,m.added_at,e.* FROM evidence_dispute_materials m "
            "JOIN evidence_documents e ON e.id=m.evidence_id WHERE m.dispute_id=? ORDER BY m.evidence_id",
            (dispute_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def open_dispute_ids_for_evidence(self, evidence_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT d.id FROM evidence_disputes d JOIN evidence_dispute_materials m ON m.dispute_id=d.id "
            "WHERE m.evidence_id=? AND d.status='open'",
            (evidence_id,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_disputes(self, *, product_id: int | None, status: str | None) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if product_id is not None:
            clauses.append("d.product_id=?")
            params.append(product_id)
        if status:
            clauses.append("d.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT d.*,p.code AS product_code,p.name AS product_name,"
            "COUNT(DISTINCT m.evidence_id) AS material_count,"
            "COUNT(DISTINCT r.id) AS ruling_count "
            "FROM evidence_disputes d JOIN health_products p ON p.id=d.product_id "
            "LEFT JOIN evidence_dispute_materials m ON m.dispute_id=d.id "
            "LEFT JOIN evidence_dispute_rulings r ON r.dispute_id=d.id" + where +
            " GROUP BY d.id ORDER BY d.updated_at DESC,d.id DESC",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def current_ruling(self, dispute_id: int) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_dispute_rulings WHERE dispute_id=? AND is_current=1",
            (dispute_id,),
        ).fetchone())

    def ruling_by_idempotency(self, dispute_id: int, key: str) -> dict | None:
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_dispute_rulings WHERE dispute_id=? AND idempotency_key=?",
            (dispute_id, key),
        ).fetchone())

    def next_ruling_revision(self, dispute_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(revision_no),0)+1 FROM evidence_dispute_rulings WHERE dispute_id=?",
            (dispute_id,),
        ).fetchone()[0])

    def supersede_current_ruling(self, dispute_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE evidence_dispute_rulings SET is_current=0,superseded_at=? WHERE dispute_id=? AND is_current=1",
            (now, dispute_id),
        )

    def insert_ruling(self, *, dispute_id: int, revision_no: int, adjudicator: str, decision: str, comparison_basis: str, applicable_scope: dict, interim_restrictions: list[str], is_final: bool, idempotency_key: str | None, now: str) -> dict:
        cursor = self.connection.execute(
            "INSERT INTO evidence_dispute_rulings(dispute_id,revision_no,adjudicator,decision,comparison_basis,"
            "applicable_scope_json,interim_restrictions_json,is_final,is_current,idempotency_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (dispute_id, revision_no, adjudicator, decision, comparison_basis,
             json.dumps(applicable_scope, ensure_ascii=False, sort_keys=True),
             json.dumps(interim_restrictions, ensure_ascii=False),
             1 if is_final else 0, idempotency_key, now),
        )
        return _dict(self.connection.execute(
            "SELECT * FROM evidence_dispute_rulings WHERE id=?", (cursor.lastrowid,)
        ).fetchone()) or {}

    def ruling_history(self, dispute_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_dispute_rulings WHERE dispute_id=? ORDER BY revision_no",
            (dispute_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_dispute_resolved(self, dispute_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE evidence_disputes SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
            (now, now, dispute_id),
        )

    def reopen_dispute(self, dispute_id: int, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE evidence_disputes SET status='open',resolved_at=NULL,reopen_reason=?,updated_at=? WHERE id=?",
            (reason, now, dispute_id),
        )

    def material_prior_status(self, dispute_id: int, evidence_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT prior_status FROM evidence_dispute_materials WHERE dispute_id=? AND evidence_id=?",
            (dispute_id, evidence_id),
        ).fetchone()
        return None if row is None else row["prior_status"]

