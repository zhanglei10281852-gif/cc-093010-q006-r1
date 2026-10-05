from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "health-innovation.db"
_local = threading.local()


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, department_id, starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS health_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    origin_country TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('康复设备','辅助诊断','数字疗法','慢病管理','数字中医','健康消费')),
    intended_use TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    regulatory_status TEXT NOT NULL DEFAULT '展示' CHECK(regulatory_status IN ('展示','研究','已注册','暂停')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site_type TEXT NOT NULL CHECK(site_type IN ('展会体验点','医院','康复机构','研究机构','产业伙伴')),
    region TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('临床','性能','安全','合规','体验')),
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_region TEXT NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected','superseded','disputed')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    claim_use TEXT,
    claim_metric TEXT,
    claim_population TEXT,
    claim_conclusion TEXT CHECK(claim_conclusion IS NULL OR claim_conclusion IN ('supports','concern')),
    claim_scope_key TEXT,
    UNIQUE(product_id, evidence_type, version, content_digest)
);
CREATE TABLE IF NOT EXISTS evidence_disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    claim_use TEXT NOT NULL,
    claim_metric TEXT NOT NULL,
    claim_population TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    opened_by TEXT NOT NULL,
    dedupe_key TEXT,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    reopen_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispute_open_scope ON evidence_disputes(product_id,scope_key) WHERE status='open';
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispute_dedupe ON evidence_disputes(dedupe_key) WHERE dedupe_key IS NOT NULL AND status='open';
CREATE TABLE IF NOT EXISTS evidence_dispute_materials (
    dispute_id INTEGER NOT NULL REFERENCES evidence_disputes(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence_documents(id) ON DELETE CASCADE,
    prior_status TEXT,
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY(dispute_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS evidence_dispute_rulings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL REFERENCES evidence_disputes(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    adjudicator TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('prefer_supports','prefer_concern','inconclusive','restricted_use')),
    comparison_basis TEXT NOT NULL,
    applicable_scope_json TEXT NOT NULL DEFAULT '{}',
    interim_restrictions_json TEXT NOT NULL DEFAULT '[]',
    is_final INTEGER NOT NULL DEFAULT 0 CHECK(is_final IN (0,1)),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
    idempotency_key TEXT,
    superseded_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(dispute_id, revision_no),
    UNIQUE(dispute_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ruling_current ON evidence_dispute_rulings(dispute_id) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS idx_ruling_dispute ON evidence_dispute_rulings(dispute_id,id);
CREATE TABLE IF NOT EXISTS public_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL REFERENCES pilot_sites(id) ON DELETE CASCADE,
    session_reference TEXT NOT NULL,
    audience_type TEXT NOT NULL CHECK(audience_type IN ('公众','临床人员','采购商','产业伙伴')),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    tags_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    contact_digest TEXT NOT NULL DEFAULT '',
    consent_to_follow_up INTEGER NOT NULL DEFAULT 0 CHECK(consent_to_follow_up IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, session_reference, audience_type, contact_digest)
);

CREATE TABLE IF NOT EXISTS pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capability TEXT NOT NULL,
    product_code TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('user','role','project')),
    subject_key TEXT NOT NULL,
    max_queued INTEGER NOT NULL CHECK(max_queued >= 0),
    max_running INTEGER NOT NULL CHECK(max_running >= 0),
    daily_submissions INTEGER NOT NULL CHECK(daily_submissions >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_key)
);
CREATE TABLE IF NOT EXISTS pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    evidence_snapshot_json TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at);
CREATE TABLE IF NOT EXISTS pilot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_interventions ON pilot_interventions(session_id,id);
'''


PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看协作团队", "departments", "read"),
    ("departments.write", "维护协作团队", "departments", "write"),
    ("catalog.read", "查看健康创新目录", "catalog", "read"),
    ("catalog.write", "维护健康创新目录", "catalog", "write"),
    ("evidence.review", "审阅产品证据", "evidence", "review"),
    ("evidence.dispute", "裁决证据争议", "evidence", "dispute"),
    ("feedback.read", "查看体验反馈", "feedback", "read"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
]


def database_path() -> Path:
    raw = os.getenv("HEALTH_INNOVATION_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(connection: sqlite3.Connection, now: str) -> None:
    """对已存在的数据库补齐新表与新列；全新数据库由 SCHEMA 直接建立。"""
    del now
    version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 3:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='evidence_documents'"
        ).fetchone()
        definition_sql = definition["sql"] if definition is not None else ""
        if definition_sql and "disputed" not in definition_sql:
            # 旧表 status 的 CHECK 不含 disputed，需要重建以放宽约束并补入结论列
            connection.execute(
                "CREATE TABLE evidence_documents_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,"
                "evidence_type TEXT NOT NULL CHECK(evidence_type IN ('临床','性能','安全','合规','体验')),"
                "title TEXT NOT NULL,source_name TEXT NOT NULL,source_region TEXT NOT NULL,"
                "version TEXT NOT NULL,content_digest TEXT NOT NULL,"
                "summary_json TEXT NOT NULL DEFAULT '{}',"
                "status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected','superseded','disputed')),"
                "submitted_by TEXT NOT NULL,submitted_at TEXT NOT NULL,reviewed_by TEXT,reviewed_at TEXT,"
                "claim_use TEXT,claim_metric TEXT,claim_population TEXT,"
                "claim_conclusion TEXT CHECK(claim_conclusion IS NULL OR claim_conclusion IN ('supports','concern')),"
                "claim_scope_key TEXT,"
                "UNIQUE(product_id, evidence_type, version, content_digest))"
            )
            connection.execute(
                "INSERT INTO evidence_documents_new(id,product_id,evidence_type,title,source_name,source_region,"
                "version,content_digest,summary_json,status,submitted_by,submitted_at,reviewed_by,reviewed_at) "
                "SELECT id,product_id,evidence_type,title,source_name,source_region,version,content_digest,"
                "summary_json,status,submitted_by,submitted_at,reviewed_by,reviewed_at FROM evidence_documents"
            )
            connection.execute("DROP TABLE evidence_documents")
            connection.execute("ALTER TABLE evidence_documents_new RENAME TO evidence_documents")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_claim_scope ON evidence_documents(product_id,claim_scope_key) "
                "WHERE claim_scope_key IS NOT NULL"
            )
        else:
            evidence_columns = _column_names(connection, "evidence_documents")
            for column, declaration in (
                ("claim_use", "claim_use TEXT"),
                ("claim_metric", "claim_metric TEXT"),
                ("claim_population", "claim_population TEXT"),
                ("claim_conclusion", "claim_conclusion TEXT"),
                ("claim_scope_key", "claim_scope_key TEXT"),
            ):
                if column not in evidence_columns:
                    connection.execute(f"ALTER TABLE evidence_documents ADD COLUMN {declaration}")
        protocol_columns = _column_names(connection, "pilot_protocols")
        if "product_code" not in protocol_columns:
            connection.execute("ALTER TABLE pilot_protocols ADD COLUMN product_code TEXT")
        session_columns = _column_names(connection, "pilot_sessions")
        if "evidence_snapshot_json" not in session_columns:
            connection.execute("ALTER TABLE pilot_sessions ADD COLUMN evidence_snapshot_json TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_claim_scope ON evidence_documents(product_id,claim_scope_key) "
        "WHERE claim_scope_key IS NOT NULL"
    )


def init_db() -> None:
    now = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)
        _migrate(connection, now)
        connection.execute("PRAGMA user_version=3")
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = [
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("operator", "试点运营员", "维护目录、场地和体验场次"),
            ("reviewer", "证据审阅员", "审阅产品证据与体验反馈"),
            ("auditor", "审计查看员", "只读查看运行与审计记录"),
        ]
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, now, now),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, now),
        )


def migrate_db() -> None:
    init_db()
