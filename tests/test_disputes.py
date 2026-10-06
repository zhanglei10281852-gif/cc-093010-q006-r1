from __future__ import annotations


PRODUCT = {
    "code": "diagnostic-ai",
    "name": "染色体辅助诊断软件",
    "organization": "示例医学科技",
    "origin_country": "中国",
    "category": "辅助诊断",
    "intended_use": "辅助临床人员完成染色体图像切割、排列和异常提示",
    "risk_level": "high",
    "regulatory_status": "研究",
}

USE = "染色体异常提示"
METRIC = "sensitivity"
POPULATION = "成人外周血样本"


def finding(direction: str, *, use: str = USE, metric: str = METRIC, population: str = POPULATION, detail: str = "") -> dict:
    return {"intended_use": use, "metric": metric, "population": population, "direction": direction, "detail": detail}


def submit_evidence(client, digest: str, *, region: str, direction: str, owner: str, title: str, findings: list[dict] | None = None, evidence_type: str = "性能") -> dict:
    payload = {
        "product_code": "diagnostic-ai",
        "evidence_type": evidence_type,
        "title": title,
        "source_name": f"{region}验证组",
        "source_region": region,
        "version": f"v-{digest[:6]}",
        "content_digest": digest,
        "summary": {"findings": findings if findings is not None else [finding(direction)]},
        "submitted_by": owner,
    }
    response = client.post("/api/catalog/evidence", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def conflicting_pair(client) -> tuple[int, int]:
    overseas = submit_evidence(
        client, "a" * 64, region="海外", direction="supportive", owner="vendor-overseas",
        title="海外性能验证报告", findings=[finding("supportive", detail="灵敏度 0.97 达标")],
    )
    domestic = submit_evidence(
        client, "b" * 64, region="中国", direction="contradictive", owner="domestic-clinic",
        title="国内临床观察报告", evidence_type="临床",
        findings=[finding("contradictive", detail="特定样本类型假阳性明显")],
    )
    return overseas["id"], domestic["id"]


def open_dispute(client, evidence_ids: list[int]) -> dict:
    response = client.post("/api/disputes/disputes", json={
        "product_code": "diagnostic-ai",
        "intended_use": USE,
        "metric": METRIC,
        "population": POPULATION,
        "evidence_ids": evidence_ids,
        "raised_by": "medical-affairs",
        "reason": "展会后两组材料结论相反",
    })
    assert response.status_code == 201, response.text
    return response.json()


def ruling_payload(*, arbiter: str = "arbiter-independent", based_on_version: int = 0, dispositions: list[dict] | None = None, restrictions: list[dict] | None = None) -> dict:
    return {
        "arbiter": arbiter,
        "comparison_basis": "比较样本量、盲法设计、人群构成与样本前处理流程",
        "applicable_scope": "成人外周血样本的染色体异常提示灵敏度评估",
        "interim_restrictions": restrictions if restrictions is not None else [],
        "dispositions": dispositions or [],
        "based_on_version": based_on_version,
        "rationale": "海外报告方法学完整但缺少国内样本类型，国内观察提示前处理差异",
    }


def test_conflict_detection_groups_opposite_findings(client):
    client.post("/api/catalog/products", json=PRODUCT)
    conflicting_pair(client)
    response = client.get("/api/disputes/conflicts?product_code=diagnostic-ai")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    conflict = items[0]
    assert conflict["metric"] == METRIC
    assert conflict["population"] == POPULATION
    assert {item["source_region"] for item in conflict["evidence"]} == {"海外", "中国"}
    assert conflict["active_dispute_code"] is None


def test_opening_dispute_freezes_materials_and_blocks_document_review(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    # 其中一份已按旧流程接受，目录会同时显示冲突材料为已接受
    review = client.post(f"/api/catalog/evidence/{overseas}/review", json={"reviewer": "reviewer-a", "decision": "accepted", "note": "来源可追溯"})
    assert review.status_code == 200

    dispute = open_dispute(client, [overseas, domestic])
    assert dispute["status"] == "open"
    statuses = {item["id"]: item["status"] for item in dispute["frozen_evidence"]}
    assert statuses == {overseas: "disputed", domestic: "disputed"}

    # 冻结后逐份审阅被拒绝
    again = client.post(f"/api/catalog/evidence/{domestic}/review", json={"reviewer": "reviewer-a", "decision": "rejected", "note": "误报过多不可采信"})
    assert again.status_code == 409

    # 重复检测同一范围不会生成第二条争议
    duplicate = client.post("/api/disputes/disputes", json={
        "product_code": "diagnostic-ai",
        "intended_use": USE, "metric": METRIC, "population": POPULATION,
        "evidence_ids": [overseas, domestic], "raised_by": "medical-affairs",
    })
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["context"]["dispute_id"] == dispute["id"]

    # 冲突视图标记材料已全部冻结，不再重复上报
    conflicts = client.get("/api/disputes/conflicts?product_code=diagnostic-ai").json()["items"]
    assert conflicts == []


def test_dispute_requires_real_opposition_and_scope(client):
    client.post("/api/catalog/products", json=PRODUCT)
    supportive = submit_evidence(client, "c" * 64, region="海外", direction="supportive", owner="owner-a", title="报告甲")
    other = submit_evidence(client, "d" * 64, region="欧洲", direction="supportive", owner="owner-b", title="报告乙")
    response = client.post("/api/disputes/disputes", json={
        "product_code": "diagnostic-ai",
        "intended_use": USE, "metric": METRIC, "population": POPULATION,
        "evidence_ids": [supportive["id"], other["id"]],
        "raised_by": "medical-affairs",
    })
    assert response.status_code == 422


def test_arbiter_must_be_conflict_free_and_dispositions_complete(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    dispute = open_dispute(client, [overseas, domestic])

    # 裁决人是材料提交方之一：利益冲突
    conflicted = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        arbiter="vendor-overseas",
        dispositions=[{"evidence_id": overseas, "outcome": "upheld"}, {"evidence_id": domestic, "outcome": "rejected"}],
    ))
    assert conflicted.status_code == 409

    # 缺少一份材料的处置
    incomplete = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        dispositions=[{"evidence_id": overseas, "outcome": "upheld"}],
    ))
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["context"]["missing_evidence_ids"] == [domestic]


def test_ruling_records_basis_scope_restrictions_and_applies_dispositions(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    dispute = open_dispute(client, [overseas, domestic])

    response = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        restrictions=[{"scene": "骨髓样本", "limitation": "暂不用于骨髓样本异常提示，需补充样本特异性验证"}],
        dispositions=[
            {"evidence_id": overseas, "outcome": "upheld", "note": "外周血场景证据可采信"},
            {"evidence_id": domestic, "outcome": "restricted", "note": "结论限定到特定样本类型"},
        ],
    ))
    assert response.status_code == 200, response.text
    ruled = response.json()
    assert ruled["status"] == "ruled"
    assert ruled["current_ruling_version"] == 1
    ruling = ruled["rulings"][0]
    assert ruling["version"] == 1
    assert ruling["based_on_version"] == 0
    assert ruling["interim_restrictions"][0]["scene"] == "骨髓样本"

    statuses = {item["id"]: item["status"] for item in ruled["frozen_evidence"]}
    assert statuses == {overseas: "accepted", domestic: "disputed"}

    landscape = client.get("/api/disputes/landscape/diagnostic-ai").json()
    assert landscape["open_disputes"] == []
    assert [item["id"] for item in landscape["available_evidence"]] == [overseas]
    assert landscape["restricted_scenes"][0]["scene"] == "骨髓样本"
    assert len(landscape["ruling_history"]) == 1


def test_concurrent_ruling_only_produces_one_version(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    dispute = open_dispute(client, [overseas, domestic])
    dispositions = [
        {"evidence_id": overseas, "outcome": "upheld"},
        {"evidence_id": domestic, "outcome": "rejected"},
    ]
    first = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(dispositions=dispositions))
    assert first.status_code == 200
    # 第二个裁决仍基于 v0：乐观锁拒绝，不能产生第二条有效结论
    second = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        arbiter="arbiter-second", dispositions=dispositions, based_on_version=0,
    ))
    assert second.status_code == 409
    assert second.json()["error"]["context"]["current_version"] == 1

    detail = client.get(f"/api/disputes/disputes/{dispute['id']}").json()
    assert [item["version"] for item in detail["rulings"]] == [1]


def test_supplement_reopens_dispute_and_revision_keeps_history(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    dispute = open_dispute(client, [overseas, domestic])
    client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        dispositions=[
            {"evidence_id": overseas, "outcome": "upheld"},
            {"evidence_id": domestic, "outcome": "restricted"},
        ],
    ))

    # 补充材料到达，涉及同一用途、指标、人群
    supplement = submit_evidence(
        client, "e" * 64, region="中国", direction="contradictive", owner="third-lab",
        title="补充多中心样本类型观察",
        findings=[finding("contradictive", population="成人外周血样本", detail="扩大样本后假阳性仍高")],
    )
    added = client.post(f"/api/disputes/disputes/{dispute['id']}/supplements", json={
        "evidence_id": supplement["id"], "added_by": "medical-affairs", "note": "补充材料要求复审", "reopen": True,
    })
    assert added.status_code == 201, added.text
    reopened = added.json()
    assert reopened["status"] == "open"
    assert reopened["current_ruling_version"] is None
    # 全部材料重新冻结
    assert all(item["status"] == "disputed" for item in reopened["frozen_evidence"])
    assert len(reopened["supplements"]) == 1

    # 无关范围的材料不能并入该争议
    unrelated = submit_evidence(
        client, "f" * 64, region="中国", direction="contradictive", owner="other-lab",
        title="另一指标观察", findings=[finding("contradictive", metric="specificity")],
    )
    rejected = client.post(f"/api/disputes/disputes/{dispute['id']}/supplements", json={
        "evidence_id": unrelated["id"], "added_by": "medical-affairs",
    })
    assert rejected.status_code == 422

    # 基于 v1 修订裁决
    revised = client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        based_on_version=1,
        restrictions=[{"scene": "骨髓样本", "limitation": "继续限制骨髓样本"}, {"scene": "陈旧样本", "limitation": "采样超过24小时限制使用"}],
        dispositions=[
            {"evidence_id": overseas, "outcome": "restricted"},
            {"evidence_id": domestic, "outcome": "upheld"},
            {"evidence_id": supplement["id"], "outcome": "upheld"},
        ],
    ))
    assert revised.status_code == 200, revised.text
    detail = revised.json()
    assert detail["current_ruling_version"] == 2
    versions = [item["version"] for item in detail["rulings"]]
    assert versions == [1, 2]
    assert detail["rulings"][1]["based_on_version"] == 1
    assert len(detail["rulings"][0]["dispositions"]) == 2  # 历史裁决内容原样保留


PROTOCOL = {
    "code": "diag-assist-demo",
    "name": "辅助诊断异常提示体验方案",
    "capability": "diagnostic-ai",
    "parameter_schema": {
        "sample_type": {"type": "string", "required": True, "choices": ["外周血样本", "骨髓样本"]},
    },
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
    "product_code": "diagnostic-ai",
}


def session_payload(key: str, *, sample_type: str = "外周血样本") -> dict:
    return {
        "protocol_code": "diag-assist-demo",
        "project_code": "expo-health-a",
        "requested_by": "pilot-operator-1",
        "parameters": {"sample_type": sample_type},
        "priority": 50,
        "idempotency_key": key,
    }


def test_disputed_evidence_cannot_support_new_sessions_but_past_sessions_keep_basis(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    created = client.post("/api/pilots/protocols?actor=administrator", json=PROTOCOL)
    assert created.status_code == 201, created.text

    # 争议未决：新场次被门禁拒绝
    dispute = open_dispute(client, [overseas, domestic])
    blocked = client.post("/api/pilots/sessions", json=session_payload("session-blocked-001"))
    assert blocked.status_code == 409
    assert "DSP-diagnostic-ai" in blocked.json()["error"]["context"]["dispute_codes"][0]

    # 裁决：海外报告在适用范围内采信，骨髓样本受限
    client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        restrictions=[{"scene": "骨髓样本", "limitation": "骨髓样本暂不开放"}],
        dispositions=[
            {"evidence_id": overseas, "outcome": "upheld"},
            {"evidence_id": domestic, "outcome": "restricted"},
        ],
    ))

    # 适用范围内：允许并固化当时判断（v1）
    allowed = client.post("/api/pilots/sessions", json=session_payload("session-allowed-001"))
    assert allowed.status_code == 202, allowed.text
    allowed_id = allowed.json()["id"]
    details = client.get(f"/api/pilots/session-details/{allowed_id}").json()
    basis = details["evidence_basis"][0]
    assert basis["product_code"] == "diagnostic-ai"
    assert basis["disputes"][0]["ruling_version"] == 1
    assert [item["id"] for item in basis["available_evidence"]] == [overseas]

    # 受限场景：即便争议已裁决也拒绝新场次
    restricted = client.post("/api/pilots/sessions", json=session_payload("session-restricted-001", sample_type="骨髓样本"))
    assert restricted.status_code == 409
    assert restricted.json()["error"]["context"]["restricted_scenes"][0]["scene"] == "骨髓样本"

    # 补充材料重开争议并修订到 v2，既有场次仍保留当时采用的 v1 判断
    supplement = submit_evidence(
        client, "ab" * 32, region="中国", direction="contradictive", owner="third-lab",
        title="补充样本类型观察", findings=[finding("contradictive")],
    )
    client.post(f"/api/disputes/disputes/{dispute['id']}/supplements", json={"evidence_id": supplement["id"], "added_by": "medical-affairs"})
    reopened_blocked = client.post("/api/pilots/sessions", json=session_payload("session-blocked-002"))
    assert reopened_blocked.status_code == 409
    client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        based_on_version=1,
        dispositions=[
            {"evidence_id": overseas, "outcome": "restricted"},
            {"evidence_id": domestic, "outcome": "upheld"},
            {"evidence_id": supplement["id"], "outcome": "upheld"},
        ],
    ))
    past = client.get(f"/api/pilots/session-details/{allowed_id}").json()
    assert past["evidence_basis"][0]["disputes"][0]["ruling_version"] == 1


def test_landscape_reports_all_four_sections(client):
    client.post("/api/catalog/products", json=PRODUCT)
    overseas, domestic = conflicting_pair(client)
    landscape = client.get("/api/disputes/landscape/diagnostic-ai").json()
    assert landscape["available_evidence"] == []
    assert len(landscape["detected_conflicts"]) == 1
    assert landscape["open_disputes"] == []
    assert landscape["restricted_scenes"] == []
    assert landscape["ruling_history"] == []

    dispute = open_dispute(client, [overseas, domestic])
    pending = client.get("/api/disputes/landscape/diagnostic-ai").json()
    assert len(pending["open_disputes"]) == 1
    assert set(pending["open_disputes"][0]["frozen_evidence_ids"]) == {overseas, domestic}

    client.post(f"/api/disputes/disputes/{dispute['id']}/rulings", json=ruling_payload(
        restrictions=[{"scene": "骨髓样本", "limitation": "限制"}],
        dispositions=[
            {"evidence_id": overseas, "outcome": "upheld"},
            {"evidence_id": domestic, "outcome": "rejected"},
        ],
    ))
    resolved = client.get("/api/disputes/landscape/diagnostic-ai").json()
    assert resolved["open_disputes"] == []
    assert len(resolved["available_evidence"]) == 1
    assert len(resolved["restricted_scenes"]) == 1
    assert len(resolved["ruling_history"]) == 1
