from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.disputes.findings import scope_digest


class DisputeRepository:
    """争议、裁决版本、补充材料与材料冻结的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def dispute_by_id(self, dispute_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        return dict(row) if row is not None else None

    def dispute_by_code(self, code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM disputes WHERE code=?", (code,)).fetchone()
        return dict(row) if row is not None else None

    def active_dispute_for_scope(self, product_id: int, digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM disputes WHERE product_id=? AND scope_digest=? AND status<>'superseded'",
            (product_id, digest),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_disputes(self, *, product_id: int | None, status: str | None) -> list[dict[str, Any]]:
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
            "SELECT d.*,p.code AS product_code,p.name AS product_name FROM disputes d "
            "JOIN health_products p ON p.id=d.product_id" + where + " ORDER BY d.raised_at DESC,d.id DESC",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_dispute(
        self,
        *,
        code: str,
        product_id: int,
        intended_use: str,
        metric: str,
        population: str,
        raised_by: str,
        now: str,
    ) -> dict[str, Any]:
        digest = scope_digest(product_id, intended_use, metric, population)
        cursor = self.connection.execute(
            "INSERT INTO disputes(code,product_id,intended_use,metric,population,scope_digest,status,raised_by,raised_at,updated_at)"
            " VALUES(?,?,?,?,?,?, 'open',?,?,?)",
            (code, product_id, intended_use, metric, population, digest, raised_by, now, now),
        )
        return self.dispute_by_id(int(cursor.lastrowid)) or {}

    def freeze_evidence(self, evidence_ids: list[int], dispute_id: int) -> None:
        self.connection.executemany(
            "UPDATE evidence_documents SET status='disputed',dispute_id=? WHERE id=? AND dispute_id IS NULL",
            [(dispute_id, evidence_id) for evidence_id in evidence_ids],
        )

    def evidence_by_id(self, evidence_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM evidence_documents WHERE id=?", (evidence_id,)).fetchone()
        return dict(row) if row is not None else None

    def evidence_for_product(self, product_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM evidence_documents WHERE product_id=? ORDER BY id",
            (product_id,),
        ).fetchall()]

    def frozen_evidence(self, dispute_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM evidence_documents WHERE dispute_id=? ORDER BY id",
            (dispute_id,),
        ).fetchall()]

    def supplements(self, dispute_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT s.*,e.title AS evidence_title,e.status AS evidence_status "
            "FROM dispute_supplements s JOIN evidence_documents e ON e.id=s.evidence_id "
            "WHERE s.dispute_id=? ORDER BY s.id",
            (dispute_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_supplement(self, dispute_id: int, evidence_id: int, added_by: str, note: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO dispute_supplements(dispute_id,evidence_id,added_by,note,created_at) VALUES(?,?,?,?,?)",
            (dispute_id, evidence_id, added_by, note, now),
        )

    def reopen_dispute(self, dispute_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE disputes SET status='open',closed_at=NULL,current_ruling_id=NULL,updated_at=? WHERE id=? AND status='ruled'",
            (now, dispute_id),
        )
        # 争议重新开放期间，全部被覆盖材料重新冻结，等待修订后的裁决
        self.connection.execute(
            "UPDATE evidence_documents SET status='disputed' WHERE dispute_id=?",
            (dispute_id,),
        )

    def latest_ruling_version(self, dispute_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM dispute_rulings WHERE dispute_id=?",
            (dispute_id,),
        ).fetchone()[0])

    def ruling_by_id(self, ruling_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM dispute_rulings WHERE id=?", (ruling_id,)).fetchone()
        return dict(row) if row is not None else None

    def add_ruling(
        self,
        *,
        dispute_id: int,
        version: int,
        arbiter: str,
        comparison_basis: str,
        applicable_scope: str,
        interim_restrictions: list[dict[str, Any]],
        basis_evidence_ids: list[int],
        dispositions: list[dict[str, Any]],
        based_on_version: int,
        rationale: str,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO dispute_rulings(dispute_id,version,arbiter,comparison_basis,applicable_scope,"
            "interim_restrictions_json,basis_evidence_ids_json,dispositions_json,based_on_version,rationale,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                dispute_id, version, arbiter, comparison_basis, applicable_scope,
                json.dumps(interim_restrictions, ensure_ascii=False, sort_keys=True),
                json.dumps(basis_evidence_ids, ensure_ascii=False),
                json.dumps(dispositions, ensure_ascii=False, sort_keys=True),
                based_on_version, rationale, now,
            ),
        )
        ruling = self.ruling_by_id(int(cursor.lastrowid)) or {}
        self.connection.execute(
            "UPDATE disputes SET status='ruled',current_ruling_id=?,closed_at=?,updated_at=? WHERE id=?",
            (ruling["id"], now, now, dispute_id),
        )
        return ruling

    def rulings(self, dispute_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM dispute_rulings WHERE dispute_id=? ORDER BY version",
            (dispute_id,),
        ).fetchall()]

    def current_ruling(self, dispute_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT r.* FROM dispute_rulings r JOIN disputes d ON d.current_ruling_id=r.id WHERE d.id=?",
            (dispute_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def active_disputes_for_product(self, product_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM disputes WHERE product_id=? AND status<>'superseded' ORDER BY id",
            (product_id,),
        ).fetchall()]

    def apply_disposition_statuses(self, dispute_id: int, dispositions: dict[int, str]) -> None:
        """裁决生效后按 disposition 回写材料状态；全部材料仍保留 dispute_id 冻结归属。"""
        for evidence_id, outcome in dispositions.items():
            status = {
                "upheld": "accepted",
                "rejected": "rejected",
                "restricted": "disputed",
            }.get(outcome, "disputed")
            self.connection.execute(
                "UPDATE evidence_documents SET status=? WHERE id=? AND dispute_id=?",
                (status, evidence_id, dispute_id),
            )
