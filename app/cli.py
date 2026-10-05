from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from app.database import close_connection, database_path, get_connection, init_db
from app.main import app


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def init_database() -> int:
    init_db()
    _print({"database": str(database_path()), "initialized": True})
    return 0


def check_database() -> int:
    init_db()
    connection = get_connection()
    _print({
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
    })
    return 0


def smoke() -> int:
    with tempfile.TemporaryDirectory(prefix="health-smoke-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "smoke.db")
        close_connection()
        with TestClient(app) as client:
            root = client.get("/")
            health = client.get("/api/system/health")
            if root.status_code != 200 or health.status_code != 200:
                _print({"root": root.text, "health": health.text})
                return 1
            _print({"root": root.json(), "health": health.json()})
        close_connection()
    return 0


def pilot_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-demo-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "demo.db")
        close_connection()
        with TestClient(app) as client:
            product = client.post("/api/catalog/products", json={
                "code": "exoskeleton-a",
                "name": "轻量助力外骨骼",
                "organization": "示例康复科技",
                "origin_country": "中国",
                "category": "康复设备",
                "intended_use": "用于展会和康复机构的步态助力体验与运行数据观察",
                "risk_level": "medium",
                "regulatory_status": "展示",
            })
            site = client.post("/api/catalog/sites", json={
                "code": "expo-hall-a",
                "name": "数智医疗体验点",
                "site_type": "展会体验点",
                "region": "杭州",
                "capabilities": ["gait-assist"],
                "max_concurrent": 2,
            })
            protocol = client.post("/api/pilots/protocols?actor=demo", json={
                "code": "gait-assist",
                "name": "外骨骼步态体验方案",
                "capability": "gait-assist",
                "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
                "default_parameters": {},
                "max_runtime_seconds": 1800,
                "max_attempts": 2,
            })
            submitted = client.post("/api/pilots/sessions", json={
                "protocol_code": "gait-assist",
                "project_code": "expo-2026",
                "requested_by": "operator-demo",
                "parameters": {"minutes": 8},
                "priority": 70,
                "idempotency_key": "demo-session-001",
            })
            claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "expo-hall-a", "capabilities": ["gait-assist"], "lease_seconds": 60})
            values = [product, site, protocol, submitted, claimed]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            _print({"product": product.json()["code"], "site": site.json()["code"], "session": claimed.json()["session"]})
        close_connection()
    return 0


def dispute_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-dispute-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "dispute.db")
        close_connection()
        with TestClient(app) as client:
            product_response = client.post("/api/catalog/products", json={
                "code": "diag-ai-demo",
                "name": "辅助诊断异常提示软件",
                "organization": "示例医学科技",
                "origin_country": "中国",
                "category": "辅助诊断",
                "intended_use": "辅助临床人员完成染色体图像异常提示",
                "risk_level": "high",
                "regulatory_status": "研究",
            })
            assert product_response.status_code == 201, product_response.text
            scope = {"use": "染色体异常提示", "metric": "灵敏度", "population": "成人外周血样本"}

            def evidence(code: str, region: str, submitted_by: str, conclusion: str) -> int:
                response = client.post("/api/catalog/evidence", json={
                    "product_code": "diag-ai-demo",
                    "evidence_type": "性能",
                    "title": f"{region}性能报告",
                    "source_name": f"{region}验证机构",
                    "source_region": region,
                    "version": f"v-{code[:6]}",
                    "content_digest": code,
                    "summary": {},
                    "submitted_by": submitted_by,
                    "claim": {**scope, "conclusion": conclusion},
                })
                assert response.status_code == 201, response.text
                return int(response.json()["id"])

            def accept(evidence_id: int, reviewer: str) -> dict:
                response = client.post(f"/api/catalog/evidence/{evidence_id}/review", json={
                    "reviewer": reviewer, "decision": "accepted", "note": "来源与版本可追溯",
                })
                assert response.status_code == 200, response.text
                return response.json()

            overseas = evidence("o" * 64, "海外", "overseas-lab", "supports")
            accept(overseas, "reviewer-a")
            domestic = evidence("d" * 64, "中国", "dominical-lab", "concern")
            accepted_domestic = accept(domestic, "reviewer-b")
            dispute_id = int(accepted_domestic["auto_dispute"]["dispute_id"])

            ruling = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
                "adjudicator": "adjudicator-neutral",
                "decision": "restricted_use",
                "comparison_basis": "海外多中心报告与国内临床观察覆盖样本类型不同，需限定场景并补充数据",
                "applicable_scope": {"sample": "国内特定样本类型"},
                "interim_restrictions": ["国内特定样本类型仅保留人工复核，暂不启用自动异常提示"],
                "idempotency_key": "demo-ruling-1",
            })
            assert ruling.status_code == 200, ruling.text

            extra = evidence("e" * 64, "中国", "followup-lab", "concern")
            reopen = client.post(f"/api/catalog/disputes/{dispute_id}/reopen", json={
                "actor": "coordinator",
                "reason": "新增国内多中心补充观察，覆盖此前缺失的样本类型",
                "evidence_ids": [extra],
            })
            assert reopen.status_code == 200, reopen.text
            revised = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
                "adjudicator": "adjudicator-neutral-2",
                "decision": "prefer_concern",
                "comparison_basis": "补充多中心材料确认特定样本类型误报明显，担忧结论证据更充分",
                "applicable_scope": {"region": "中国", "sample": "特定样本类型"},
                "interim_restrictions": ["暂停该样本类型自动异常提示直至产品方完成整改"],
            })
            assert revised.status_code == 200, revised.text
            overview = client.get("/api/catalog/evidence-overview/diag-ai-demo")
            assert overview.status_code == 200, overview.text
            _print({
                "dispute_code": overview.json()["ruling_history"][0]["dispute_code"],
                "revisions": len(overview.json()["ruling_history"][0]["rulings"]),
                "restricted_scenarios": len(overview.json()["restricted_scenarios"]),
                "new_pilot_blocked": overview.json()["new_pilot_blocked"],
            })
        close_connection()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全球健康创新试点运营服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="初始化 SQLite 数据库")
    sub.add_parser("check-db", help="检查数据库完整性")
    sub.add_parser("smoke", help="进程内检查根路径和健康接口")
    sub.add_parser("pilot-demo", help="运行产品、场地、方案和场次演示")
    sub.add_parser("dispute-demo", help="运行证据冲突、裁决、重开与修订演示")
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    actions = {"init-db": init_database, "check-db": check_database, "smoke": smoke, "pilot-demo": pilot_demo, "dispute-demo": dispute_demo}
    try:
        return actions[command]()
    finally:
        close_connection()


if __name__ == "__main__":
    sys.exit(main())

