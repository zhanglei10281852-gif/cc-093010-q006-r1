from __future__ import annotations

import sqlite3
import threading

import pytest

from app.catalog.disputes import DisputeService
from app.database import close_connection, get_connection, init_db


PRODUCT = {
    "code": "diag-ai-01",
    "name": "辅助诊断异常提示软件",
    "organization": "示例医学科技",
    "origin_country": "中国",
    "category": "辅助诊断",
    "intended_use": "辅助临床人员完成染色体图像异常提示",
    "risk_level": "high",
    "regulatory_status": "研究",
}

SCOPE = {
    "use": "染色体异常提示",
    "metric": "灵敏度",
    "population": "成人外周血样本",
}


def evidence_payload(digest: str, *, conclusion: str, region: str, submitted_by: str, etype="性能", metric="灵敏度"):
    return {
        "product_code": PRODUCT["code"],
        "evidence_type": etype,
        "title": f"{region}{etype}报告",
        "source_name": f"{region}来源",
        "source_region": region,
        "version": f"v-{digest[:6]}",
        "content_digest": digest,
        "summary": {},
        "submitted_by": submitted_by,
        "claim": {"use": SCOPE["use"], "metric": metric, "population": SCOPE["population"], "conclusion": conclusion},
    }


def setup_product(client):
    resp = client.post("/api/catalog/products", json=PRODUCT)
    assert resp.status_code == 201, resp.text


def submit_pair(client):
    """海外 supports 与国内 concern 两份材料，均通过审阅。"""
    setup_product(client)
    overseas = client.post("/api/catalog/evidence", json=evidence_payload("o" * 64, conclusion="supports", region="海外", submitted_by="overseas-lab"))
    domestic = client.post("/api/catalog/evidence", json=evidence_payload("d" * 64, conclusion="concern", region="中国", submitted_by="dominical-lab"))
    assert overseas.status_code == domestic.status_code == 201
    r1 = client.post(f"/api/catalog/evidence/{overseas.json()['id']}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "来源可追溯"})
    assert r1.status_code == 200
    return overseas.json()["id"], domestic.json()["id"]


def test_accepting_conflicting_claims_auto_opens_dispute_and_freezes(client):
    overseas_id, domestic_id = submit_pair(client)
    second = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "临床观察可信"})
    assert second.status_code == 200, second.text
    auto = second.json()["auto_dispute"]
    assert auto is not None
    assert set(auto["frozen_evidence_ids"]) == {overseas_id, domestic_id}
    dispute = client.get(f"/api/catalog/disputes/{auto['dispute_id']}").json()
    assert dispute["status"] == "open"
    assert {m["evidence_id"] for m in dispute["materials"]} == {overseas_id, domestic_id}
    assert {m["conclusion"] for m in dispute["materials"]} == {"supports", "concern"}
    assert {m["frozen_status"] for m in dispute["materials"]} == {"disputed"}
    # 冻结后不能再逐份接受/驳回
    again = client.post(f"/api/catalog/evidence/{overseas_id}/review", json={"reviewer": "reviewer-a", "decision": "rejected", "note": "尝试改判"})
    assert again.status_code == 409
    listing = client.get(f"/api/catalog/evidence?product_code={PRODUCT['code']}").json()["items"]
    assert all(item["id"] not in (overseas_id, domestic_id) or item["status"] == "disputed" for item in listing)


def test_different_population_does_not_conflict(client):
    setup_product(client)
    e1 = client.post("/api/catalog/evidence", json=evidence_payload("a" * 64, conclusion="supports", region="海外", submitted_by="lab-a"))
    other = evidence_payload("b" * 64, conclusion="concern", region="中国", submitted_by="lab-b")
    other["claim"]["population"] = "新生儿脐带血样本"
    e2 = client.post("/api/catalog/evidence", json=other)
    client.post(f"/api/catalog/evidence/{e1.json()['id']}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "ok"})
    second = client.post(f"/api/catalog/evidence/{e2.json()['id']}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"})
    assert second.json()["auto_dispute"] is None
    disputes = client.get(f"/api/catalog/disputes?product_code={PRODUCT['code']}").json()["items"]
    assert disputes == []


def test_manual_open_without_two_sides_rejected(client):
    setup_product(client)
    only = client.post("/api/catalog/evidence", json=evidence_payload("a" * 64, conclusion="supports", region="海外", submitted_by="lab-a"))
    client.post(f"/api/catalog/evidence/{only.json()['id']}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "ok"})
    resp = client.post("/api/catalog/disputes", json={
        "product_code": PRODUCT["code"], **SCOPE, "opened_by": "coordinator",
    })
    assert resp.status_code == 422


def test_conflict_of_interest_blocks_submitter_and_reviewer(client):
    _, domestic_id = submit_pair(client)
    second = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"})
    dispute_id = second.json()["auto_dispute"]["dispute_id"]
    ruling = {
        "decision": "prefer_supports",
        "comparison_basis": "海外多中心样本量更大，方法学更完整；国内观察样本类型单一",
        "applicable_scope": {"region": "海外"},
        "interim_restrictions": ["国内特定样本类型暂不启用异常提示"],
    }
    as_submitter = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={**ruling, "adjudicator": "dominical-lab"})
    assert as_submitter.status_code == 409
    as_reviewer = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={**ruling, "adjudicator": "reviewer-a"})
    assert as_reviewer.status_code == 409
    independent = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={**ruling, "adjudicator": "adjudicator-neutral"})
    assert independent.status_code == 200, independent.text
    body = independent.json()
    assert body["current_ruling"]["adjudicator"] == "adjudicator-neutral"
    assert body["current_ruling"]["comparison_basis"].startswith("海外")
    assert body["status"] == "resolved"


def test_restricted_use_requires_restrictions(client):
    _, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    resp = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral",
        "decision": "restricted_use",
        "comparison_basis": "双方样本量均有限，需要补充多中心数据后再判定",
        "interim_restrictions": [],
    })
    assert resp.status_code == 422


def test_concurrent_and_duplicate_ruling_yield_single_current(client):
    _, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    payload = {
        "adjudicator": "neutral",
        "decision": "prefer_concern",
        "comparison_basis": "国内临床观察针对目标样本类型，误报率结论应优先适用",
        "interim_restrictions": ["限定海外结论适用范围之外的场景需复核"],
        "idempotency_key": "ruling-001",
    }
    first = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json=payload)
    assert first.status_code == 200
    # 相同幂等键重放：返回原结论，不新增修订
    duplicate = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["current_ruling"]["revision_no"] == 1
    # 无幂等键在已解决争议上再次裁决：拒绝，而不是产生第二条结论
    again = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral",
        "decision": "prefer_supports",
        "comparison_basis": "另一位审阅人尝试给出相反裁决",
    })
    assert again.status_code == 409
    rows = get_connection().execute(
        "SELECT COUNT(*) AS c FROM evidence_dispute_rulings WHERE dispute_id=? AND is_current=1",
        (dispute_id,),
    ).fetchone()
    assert rows["c"] == 1
    total = get_connection().execute(
        "SELECT COUNT(*) AS c FROM evidence_dispute_rulings WHERE dispute_id=?", (dispute_id,)
    ).fetchone()
    assert total["c"] == 1


def test_database_enforces_single_current_ruling(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_INNOVATION_DATABASE_PATH", str(tmp_path / "concurrency.db"))
    close_connection()
    init_db()
    connection = get_connection()
    connection.execute(
        "INSERT INTO health_products(code,name,organization,origin_country,category,intended_use,risk_level,regulatory_status,active,created_at,updated_at) "
        "VALUES('p','n','o','c','辅助诊断','u','high','研究',1,'t','t')"
    )
    connection.execute(
        "INSERT INTO evidence_disputes(code,product_id,claim_use,claim_metric,claim_population,scope_key,status,opened_by,opened_at,created_at,updated_at) "
        "VALUES('d',1,'u','m','pop','k','open','x','t','t','t')"
    )
    connection.execute(
        "INSERT INTO evidence_dispute_rulings(dispute_id,revision_no,adjudicator,decision,comparison_basis,is_current,created_at) "
        "VALUES(1,1,'a','inconclusive','basis',1,'t')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO evidence_dispute_rulings(dispute_id,revision_no,adjudicator,decision,comparison_basis,is_current,created_at) "
            "VALUES(1,2,'b','prefer_supports','basis',1,'t')"
        )
    close_connection()


def test_resolution_restores_evidence_and_reopen_adds_revision(client):
    overseas_id, domestic_id = submit_pair(client)
    second = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"})
    dispute_id = second.json()["auto_dispute"]["dispute_id"]
    client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral",
        "decision": "prefer_supports",
        "comparison_basis": "海外多中心研究方法学更完整，样本量显著更大",
        "applicable_scope": {"region": "海外多中心"},
        "interim_restrictions": ["国内样本类型需补充验证"],
    })
    evidence = {item["id"]: item["status"] for item in client.get(f"/api/catalog/evidence?product_code={PRODUCT['code']}").json()["items"]}
    assert evidence[overseas_id] == "accepted"
    assert evidence[domestic_id] == "rejected"
    # 补充材料到达后重开
    extra = client.post("/api/catalog/evidence", json=evidence_payload("e" * 64, conclusion="concern", region="中国", submitted_by="followup-lab"))
    reopen = client.post(f"/api/catalog/disputes/{dispute_id}/reopen", json={
        "actor": "coordinator", "reason": "新增国内多中心补充观察报告", "evidence_ids": [extra.json()["id"]],
    })
    assert reopen.status_code == 200, reopen.text
    body = reopen.json()
    assert body["status"] == "open"
    assert any(m["evidence_id"] == extra.json()["id"] and m["frozen_status"] == "disputed" for m in body["materials"])
    assert body["current_ruling"] is None
    # 修订裁决，完整历史保留两版
    revised = client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral-2",
        "decision": "restricted_use",
        "comparison_basis": "补充材料证实特定样本类型误报，限制使用范围而非整体否定",
        "applicable_scope": {"region": "中国", "sample": "特定样本类型"},
        "interim_restrictions": ["特定样本类型关闭自动异常提示，仅人工复核"],
    })
    assert revised.status_code == 200
    detail = client.get(f"/api/catalog/disputes/{dispute_id}").json()
    revisions = detail["rulings"]
    assert [r["revision_no"] for r in revisions] == [1, 2]
    assert revisions[0]["is_current"] is False and revisions[0]["superseded_at"]
    assert revisions[1]["is_current"] is True
    assert revisions[1]["decision"] == "restricted_use"


def test_disputed_evidence_blocks_new_protocol_but_keeps_existing_snapshot(client):
    _, domestic_id = submit_pair(client)
    protocol_before = client.post("/api/pilots/protocols?actor=operator", json={
        "code": "diag-proto", "name": "辅助诊断体验方案", "capability": "diag", "product_code": PRODUCT["code"],
        "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
        "default_parameters": {},
    })
    assert protocol_before.status_code == 201, protocol_before.text
    first_session = client.post("/api/pilots/sessions", json={
        "protocol_code": "diag-proto", "project_code": "proj", "requested_by": "operator",
        "parameters": {"minutes": 10}, "priority": 50, "idempotency_key": "session-000001",
    })
    assert first_session.status_code == 202
    # 随后产生未决争议
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    blocked_protocol = client.post("/api/pilots/protocols?actor=operator", json={
        "code": "diag-proto-2", "name": "第二个方案", "capability": "diag2", "product_code": PRODUCT["code"],
        "parameter_schema": {"minutes": {"type": "integer"}},
    })
    assert blocked_protocol.status_code == 409
    blocked_session = client.post("/api/pilots/sessions", json={
        "protocol_code": "diag-proto", "project_code": "proj", "requested_by": "operator",
        "parameters": {"minutes": 12}, "priority": 50, "idempotency_key": "session-000002",
    })
    assert blocked_session.status_code == 409
    # 既有场次保留当时判断（快照中无未决争议）
    details = client.get("/api/pilots/session-details/{}".format(first_session.json()["id"])).json()
    assert details["evidence_snapshot"] is not None
    assert details["evidence_snapshot"]["open_disputes"] == []
    # 裁决受限后，新方案仍然阻断；既有场次快照不变
    client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral", "decision": "prefer_concern",
        "comparison_basis": "国内观察覆盖目标人群，结论优先", "interim_restrictions": ["暂停特定样本类型自动提示"],
    })
    still_blocked = client.post("/api/pilots/protocols?actor=operator", json={
        "code": "diag-proto-3", "name": "第三个方案", "capability": "diag3", "product_code": PRODUCT["code"],
        "parameter_schema": {"minutes": {"type": "integer"}},
    })
    assert still_blocked.status_code == 409
    details_after = client.get("/api/pilots/session-details/{}".format(first_session.json()["id"])).json()
    assert details_after["evidence_snapshot"]["open_disputes"] == []


def test_overview_reports_usable_pending_restricted_and_history(client):
    overseas_id, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    pending = client.get(f"/api/catalog/evidence-overview/{PRODUCT['code']}").json()
    assert pending["new_pilot_blocked"] is True
    assert len(pending["pending_conflicts"]) == 1
    assert pending["pending_conflicts"][0]["dispute_code"].startswith("DSP-")
    assert pending["restricted_scenarios"] == []
    # 未决期间双方都不算可用证据
    assert pending["usable_evidence"] == []
    client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral", "decision": "restricted_use",
        "comparison_basis": "双方各有适用边界，需要限定使用场景并补充数据",
        "applicable_scope": {"sample": "特定样本类型"},
        "interim_restrictions": ["特定样本类型仅人工复核"],
    })
    overview = client.get(f"/api/catalog/evidence-overview/{PRODUCT['code']}").json()
    assert overview["pending_conflicts"] == []
    assert len(overview["restricted_scenarios"]) == 1
    assert overview["restricted_scenarios"][0]["interim_restrictions"] == ["特定样本类型仅人工复核"]
    assert overview["new_pilot_blocked"] is True
    history = overview["ruling_history"][0]
    assert history["scope"]["population"] == SCOPE["population"]
    assert len(history["rulings"]) == 1
    assert history["rulings"][0]["comparison_basis"].startswith("双方")


def test_unbound_protocol_unaffected_by_disputes(client):
    _, domestic_id = submit_pair(client)
    client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"})
    proto = client.post("/api/pilots/protocols?actor=operator", json={
        "code": "generic-proto", "name": "通用方案", "capability": "generic",
        "parameter_schema": {"minutes": {"type": "integer"}},
    })
    assert proto.status_code == 201
    session = client.post("/api/pilots/sessions", json={
        "protocol_code": "generic-proto", "project_code": "proj", "requested_by": "operator",
        "parameters": {"minutes": 5}, "priority": 50, "idempotency_key": "generic-session-1",
    })
    assert session.status_code == 202
    assert session.json().get("evidence_snapshot_json") is None


def test_new_conflicting_material_during_open_dispute_gets_attached(client):
    overseas_id, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    # 未决期间又提交并接受一份同范围 supports 材料
    third = client.post("/api/catalog/evidence", json=evidence_payload("f" * 64, conclusion="supports", region="欧洲", submitted_by="eu-lab"))
    accepted = client.post(f"/api/catalog/evidence/{third.json()['id']}/review", json={"reviewer": "reviewer-c", "decision": "accepted", "note": "补充支持材料"})
    assert accepted.status_code == 200
    auto = accepted.json()["auto_dispute"]
    assert auto["action"] == "attached" and auto["dispute_id"] == dispute_id
    detail = client.get(f"/api/catalog/disputes/{dispute_id}").json()
    assert any(m["evidence_id"] == third.json()["id"] and m["frozen_status"] == "disputed" for m in detail["materials"])
    # 材料被冻结，不能逐份再审阅
    again = client.post(f"/api/catalog/evidence/{third.json()['id']}/review", json={"reviewer": "reviewer-c", "decision": "rejected", "note": "尝试改判驳回"})
    assert again.status_code == 409


def test_conflict_after_resolution_reopens_same_dispute_with_history(client):
    overseas_id, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    client.post(f"/api/catalog/disputes/{dispute_id}/rulings", json={
        "adjudicator": "neutral", "decision": "prefer_supports",
        "comparison_basis": "首轮：海外多中心证据更充分", "interim_restrictions": ["国内场景先观察"],
    })
    # 裁决后新的国内多中心 concern 材料被接受，冲突再次成立
    followup = client.post("/api/catalog/evidence", json=evidence_payload("g" * 64, conclusion="concern", region="中国", submitted_by="cn-multicenter"))
    accepted = client.post(f"/api/catalog/evidence/{followup.json()['id']}/review", json={"reviewer": "reviewer-d", "decision": "accepted", "note": "新多中心观察"})
    auto = accepted.json()["auto_dispute"]
    assert auto["action"] == "reopened" and auto["dispute_id"] == dispute_id
    detail = client.get(f"/api/catalog/disputes/{dispute_id}").json()
    assert detail["status"] == "open"
    assert detail["current_ruling"] is None
    assert [r["revision_no"] for r in detail["rulings"]] == [1]
    assert detail["rulings"][0]["is_current"] is False
    # 仍然只有一个争议，没有为同范围新开第二条
    disputes = client.get(f"/api/catalog/disputes?product_code={PRODUCT['code']}").json()["items"]
    assert len(disputes) == 1


def test_concurrent_open_dispute_collapses_to_one(client):
    overseas_id, domestic_id = submit_pair(client)
    client.post(f"/api/catalog/evidence/{overseas_id}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "ok"})
    client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"})
    # 自动争议可能已经产生；先确认，然后改为并发手动开启到另一独立范围
    results: list[dict | Exception] = []

    def worker() -> None:
        close_connection()  # 确保工作线程使用自己的连接
        service = DisputeService()
        try:
            value = service.open_dispute({
                "product_code": PRODUCT["code"], **SCOPE, "opened_by": "coordinator",
            })
            results.append(value)
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # 两个并发请求要么折叠到同一争议，要么其中一个冲突，绝不能产生两个未决争议
    open_disputes = client.get(f"/api/catalog/disputes?product_code={PRODUCT['code']}&status=open").json()["items"]
    assert len(open_disputes) == 1
    codes = {item["code"] for item in results if isinstance(item, dict)}
    if codes:
        assert codes == {open_disputes[0]["code"]}


def test_concurrent_ruling_yields_single_valid_conclusion(client):
    _, domestic_id = submit_pair(client)
    dispute_id = client.post(f"/api/catalog/evidence/{domestic_id}/review", json={"reviewer": "reviewer-b", "decision": "accepted", "note": "ok"}).json()["auto_dispute"]["dispute_id"]
    outcomes: list[str] = []

    def worker(adjudicator: str, decision: str) -> None:
        close_connection()
        service = DisputeService()
        try:
            service.rule(dispute_id, {
                "adjudicator": adjudicator,
                "decision": decision,
                "comparison_basis": "并发提交的比较依据，需要足够长度以通过校验",
                "applicable_scope": {},
                "interim_restrictions": ["临时限制一"],
                "idempotency_key": None,
            })
            outcomes.append("ok")
        except Exception:  # noqa: BLE001
            outcomes.append("conflict")

    t1 = threading.Thread(target=worker, args=("neutral-one", "prefer_supports"))
    t2 = threading.Thread(target=worker, args=("neutral-two", "prefer_concern"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes).count("ok") == 1
    assert "conflict" in outcomes
    rows = get_connection().execute(
        "SELECT COUNT(*) AS c FROM evidence_dispute_rulings WHERE dispute_id=? AND is_current=1",
        (dispute_id,),
    ).fetchone()
    assert rows["c"] == 1
    detail = client.get(f"/api/catalog/disputes/{dispute_id}").json()
    assert detail["status"] == "resolved"
