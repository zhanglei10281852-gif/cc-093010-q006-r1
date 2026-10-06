"""证据结论（finding）归一化与冲突识别。

证据材料在 ``summary.findings`` 中结构化声明其对某一用途、指标、适用人群的
结论方向。同一产品、用途、指标、适用人群下同时存在支持与反对结论即构成冲突。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

SUPPORTIVE = "supportive"
CONTRADICTIVE = "contradictive"

DIRECTION_LABELS = {
    SUPPORTIVE: "支持达标",
    CONTRADICTIVE: "提示异常",
}


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def scope_digest(product_id: int, intended_use: str, metric: str, population: str) -> str:
    payload = [
        int(product_id),
        normalize_text(intended_use),
        normalize_text(metric),
        normalize_text(population),
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_findings(summary_json: str) -> list[dict[str, str]]:
    try:
        summary = json.loads(summary_json or "{}")
    except (TypeError, ValueError):
        return []
    findings: list[dict[str, str]] = []
    for item in summary.get("findings", []) or []:
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction", "")).strip()
        if direction not in DIRECTION_LABELS:
            continue
        intended_use = str(item.get("intended_use", "")).strip()
        metric = str(item.get("metric", "")).strip()
        population = str(item.get("population", "")).strip()
        if not intended_use or not metric or not population:
            continue
        findings.append({
            "intended_use": intended_use,
            "metric": metric,
            "population": population,
            "direction": direction,
            "detail": str(item.get("detail", "")).strip(),
        })
    return findings


def scopes_of_document(document: dict[str, Any]) -> dict[str, dict[str, str]]:
    """返回证据覆盖的适用范围：scope_digest -> 归一化范围（含方向集合）。"""
    scopes: dict[str, dict[str, Any]] = {}
    for finding in extract_findings(document["summary_json"]):
        digest = scope_digest(document["product_id"], finding["intended_use"], finding["metric"], finding["population"])
        scope = scopes.setdefault(digest, {**finding, "directions": set()})
        scope["directions"].add(finding["direction"])
    return scopes


def conflict_groups(documents: Iterable[dict[str, Any]], *, include_settled: bool = False) -> dict[str, dict[str, Any]]:
    """按适用范围聚合材料，返回同时出现支持与反对结论的范围。

    默认不重复报告已被同一有效争议全部冻结的范围；裁决后又出现未冻结的相反材料时，
    该范围仍会被识别为冲突，并带出既有争议编号。开立争议校验时传 include_settled
    以在材料已冻结的情况下仍定位到原范围，从而拒绝重复开立。
    """
    groups: dict[str, dict[str, Any]] = {}
    for document in documents:
        for digest, scope in scopes_of_document(document).items():
            group = groups.setdefault(digest, {
                "scope_digest": digest,
                "product_id": int(document["product_id"]),
                "intended_use": scope["intended_use"],
                "metric": scope["metric"],
                "population": scope["population"],
                "evidence_ids": set(),
                "directions": set(),
                "active_dispute_ids": set(),
                "has_unfrozen": False,
            })
            group["evidence_ids"].add(int(document["id"]))
            group["directions"].update(scope["directions"])
            if document.get("dispute_id"):
                group["active_dispute_ids"].add(int(document["dispute_id"]))
            else:
                group["has_unfrozen"] = True
    result: dict[str, dict[str, Any]] = {}
    for digest, group in groups.items():
        if SUPPORTIVE not in group["directions"] or CONTRADICTIVE not in group["directions"]:
            continue
        if include_settled:
            result[digest] = group
            continue
        if group["active_dispute_ids"] and not group["has_unfrozen"]:
            continue
        result[digest] = group
    return result
