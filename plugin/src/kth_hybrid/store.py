"""可靠原件仓与 Case 记录库。

设计依据（v3 计划 T03 + 2026-09-08 首次 I/O 复核实测）：
- Windows 上目录级 ``os.replace`` 到已存在目标确定性失败（WinError 5，重试无效），
  文件级 replace 正常——因此提交机制是 **文件先持久化 + os.replace（文件级）+
  SQLite 事务提交引用**，不依赖目录整体替换。
- 同内容去重：内容寻址（sha256）决定对象身份；元数据/引用存 SQLite。
- ``read_bytes`` 重核内容，截断/篡改可见失败。
- 原件落盘与 DB 提交之间崩溃产生"孤立工件"，保留不删，可被识别。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from threading import RLock

from .contracts import CASE_STAGES, BlobRef, is_sha256_hex, sha256_hex

_SCHEMA_VERSION = "kth-hybrid.store.v2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_basis (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    subject_legal_name TEXT NOT NULL,
    subject_aliases TEXT NOT NULL,
    evidence_cutoff TEXT NOT NULL,
    subject_source_basis TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS source_time_evidence (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS capture_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES import_records(import_id),
    dep_name TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capdep_import ON capture_dependencies(import_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    input_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS stages (
    stage TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN
        ('pending','running','succeeded','blocked','failed')),
    run_id INTEGER,
    detail TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS import_records (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    origin_path TEXT NOT NULL,
    origin_sha256 TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    blob_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    media_type TEXT,
    locator TEXT,
    retrieved_at TEXT,
    published_at TEXT,
    published_at_provenance TEXT,
    time_evidence TEXT,
    document_subject TEXT,
    document_subject_basis TEXT,
    source_family TEXT,
    capture_status TEXT NOT NULL,
    import_id INTEGER REFERENCES import_records(import_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    locator_kind TEXT NOT NULL CHECK (locator_kind IN ('byte_range','zip_member','pdf_page')),
    locator_start INTEGER,
    locator_end INTEGER,
    locator_ref TEXT,
    excerpt_sha256 TEXT NOT NULL,
    excerpt_text TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    interpretation_attempt TEXT,
    input_digest TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS qualifications (
    qual_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    source_judgment TEXT NOT NULL,
    identity_judgment TEXT NOT NULL,
    time_judgment TEXT NOT NULL,
    independence_judgment TEXT NOT NULL,
    allowed_uses TEXT NOT NULL,
    cannot_prove TEXT NOT NULL,
    review_attempt TEXT,
    status TEXT NOT NULL CHECK (status IN ('qualified','rejected','needs_review')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS gaps (
    gap_id TEXT PRIMARY KEY,
    gap_type TEXT NOT NULL,
    affected_criteria TEXT NOT NULL,
    pipeline_fault INTEGER NOT NULL DEFAULT 0,
    investigation TEXT NOT NULL,
    unconfirmed TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS criterion_results (
    result_id TEXT PRIMARY KEY,
    criterion_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    native_disposition TEXT,
    native_note TEXT,
    product_status TEXT NOT NULL CHECK (product_status IN
        ('succeeded','insufficient','method_unsupported','execution_failed')),
    evidence_refs TEXT NOT NULL,
    gap_refs TEXT NOT NULL,
    qual_refs TEXT NOT NULL,
    rationale TEXT NOT NULL,
    scope TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);
CREATE INDEX IF NOT EXISTS idx_quals_claim ON qualifications(claim_id);
"""


class StoreIntegrityError(RuntimeError):
    """内容身份不符（截断/篡改/引用断裂）——必须可见失败。"""


class BlobStore:
    """内容寻址原件仓：同字节唯一身份，读时重核。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        self._lock = RLock()

    def _object_path(self, sha256: str) -> Path:
        if not is_sha256_hex(sha256):
            raise StoreIntegrityError(f"非法对象 id：{sha256!r}")
        path = (self.root / "objects" / sha256[:2] / sha256).resolve()
        if not path.is_relative_to((self.root / "objects").resolve()):
            raise StoreIntegrityError(f"目录逃逸：{sha256!r}")
        return path

    def put_bytes(self, data: bytes) -> BlobRef:
        digest = sha256_hex(data)
        ref = BlobRef(sha256=digest, byte_length=len(data))
        final = self._object_path(digest)
        if final.exists():
            existing = final.read_bytes()
            if existing != data:
                raise StoreIntegrityError(
                    f"对象 {digest} 已存在但内容不一致（疑似哈希碰撞或篡改）"
                )
            return ref
        final.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + fsync + 文件级 os.replace（实测可用；目录级替换不可用）
        fd, tmp_name = tempfile.mkstemp(dir=str(final.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        # 提交后重核，防止替换过程静默损坏
        if final.read_bytes() != data:
            raise StoreIntegrityError(f"对象 {digest} 落盘后复核不一致")
        return ref

    def has(self, sha256: str) -> bool:
        try:
            return self._object_path(sha256).exists()
        except StoreIntegrityError:
            return False

    def read_bytes(self, ref: BlobRef | str) -> bytes:
        sha256 = ref.sha256 if isinstance(ref, BlobRef) else ref
        data = self._object_path(sha256).read_bytes()
        digest = sha256_hex(data)
        if digest != sha256 or len(data) != (
            ref.byte_length if isinstance(ref, BlobRef) else len(data)
        ):
            raise StoreIntegrityError(
                f"对象 {sha256} 复核失败：实际 sha256={digest}，字节长度={len(data)}"
            )
        return data

    def read_range(self, ref: BlobRef | str, start: int, end: int) -> bytes:
        data = self.read_bytes(ref)
        if not (0 <= start < end <= len(data)):
            raise StoreIntegrityError(f"越界定位 [{start},{end})，对象长度 {len(data)}")
        return data[start:end]

    def list_objects(self) -> set[str]:
        objects = self.root / "objects"
        found: set[str] = set()
        for shard in objects.iterdir():
            if shard.is_dir() and len(shard.name) == 2:
                for obj in shard.iterdir():
                    if obj.is_file() and is_sha256_hex(obj.name):
                        found.add(obj.name)
        return found


class CaseStore:
    """Case 记录库（SQLite 事务台账：阶段、来源、主张、资格、缺口、判据结果）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """已建库的增量列迁移（v1→v2：R1.1 修复所需列与表）。"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS case_basis (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                subject_legal_name TEXT NOT NULL,
                subject_aliases TEXT NOT NULL,
                evidence_cutoff TEXT NOT NULL,
                subject_source_basis TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS source_time_evidence (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS capture_dependencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES import_records(import_id),
                dep_name TEXT NOT NULL,
                blob_sha256 TEXT NOT NULL,
                byte_length INTEGER NOT NULL
            );
        """)
        columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(sources)").fetchall()}
        for column in ("time_evidence", "document_subject",
                       "document_subject_basis"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
        claim_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(claims)").fetchall()}
        if "input_digest" not in claim_columns:
            self._conn.execute("ALTER TABLE claims ADD COLUMN input_digest TEXT")
        self._conn.commit()

    # ---- 阶段与运行 ----

    def new_run(self, input_digest: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs(input_digest) VALUES (?)", (input_digest,)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def init_stages(self, run_id: int) -> None:
        with self._conn:
            for stage in CASE_STAGES:
                self._conn.execute(
                    "INSERT OR IGNORE INTO stages(stage, state, run_id) VALUES (?, 'pending', ?)",
                    (stage, run_id),
                )

    def stage_state(self, stage: str) -> tuple[str, int | None]:
        row = self._conn.execute(
            "SELECT state, run_id FROM stages WHERE stage = ?", (stage,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未知阶段 {stage}")
        return (row["state"], row["run_id"])

    def set_stage(self, stage: str, state: str, run_id: int | None = None,
                  detail: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE stages SET state=?, run_id=COALESCE(?, run_id), detail=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE stage=?",
                (state, run_id, detail, stage),
            )

    # ---- CaseBasis（有源主体与截止）----

    def set_case_basis(self, *, subject_legal_name: str,
                       subject_aliases: list[str], evidence_cutoff: str,
                       subject_source_basis: str, note: str | None = None) -> None:
        """登记本Case唯一评估依据；主体来源依据必填（防无源硬编码主体）。"""
        if not subject_source_basis or not subject_source_basis.strip():
            raise ValueError("case_basis 必须携带主体来源依据（subject_source_basis）")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO case_basis(id, subject_legal_name, "
                "subject_aliases, evidence_cutoff, subject_source_basis, note) "
                "VALUES (1,?,?,?,?,?)",
                (subject_legal_name,
                 json.dumps(subject_aliases, ensure_ascii=False),
                 evidence_cutoff, subject_source_basis, note),
            )

    def get_case_basis(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM case_basis WHERE id=1").fetchone()
        if row is None:
            return None
        basis = dict(row)
        basis["subject_aliases"] = json.loads(basis["subject_aliases"])
        return basis

    # ---- 时间证据（追加版本，不改写历史）----

    def append_time_evidence(self, source_id: str, evidence: dict) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO source_time_evidence(source_id, evidence_json) "
                "VALUES (?,?)",
                (source_id, json.dumps(evidence, ensure_ascii=False)),
            )
        return int(cur.lastrowid)

    def get_time_evidence_history(self, source_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT revision, evidence_json, created_at FROM source_time_evidence "
            "WHERE source_id=? ORDER BY revision", (source_id,),
        ).fetchall()
        history = []
        for row in rows:
            entry = {"revision": row["revision"],
                     "created_at": row["created_at"]}
            entry.update(json.loads(row["evidence_json"]))
            history.append(entry)
        return history

    def latest_time_evidence(self, source_id: str) -> dict | None:
        history = self.get_time_evidence_history(source_id)
        return history[-1] if history else None

    # ---- 采集依赖封存（真实blob引用，非文件名清单）----

    def add_capture_dependency(self, import_id: int, dep_name: str,
                               blob_sha256: str, byte_length: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO capture_dependencies(import_id, dep_name, "
                "blob_sha256, byte_length) VALUES (?,?,?,?)",
                (import_id, dep_name, blob_sha256, byte_length),
            )

    def get_capture_dependencies(self, import_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT dep_name, blob_sha256, byte_length FROM capture_dependencies "
            "WHERE import_id=? ORDER BY dep_name", (import_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 记录写入（事务）----

    def add_import_record(self, kind: str, origin_path: str, origin_sha256: str | None,
                          note: str | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO import_records(kind, origin_path, origin_sha256, note) "
                "VALUES (?,?,?,?)",
                (kind, origin_path, origin_sha256, note),
            )
        return int(cur.lastrowid)

    def add_source(self, source_id: str, blob_sha256: str, byte_length: int, *,
                   media_type: str | None = None, locator: str | None = None,
                   retrieved_at: str | None = None, published_at: str | None = None,
                   published_at_provenance: str | None = None,
                   time_evidence: dict | None = None,
                   document_subject: str | None = None,
                   document_subject_basis: str | None = None,
                   source_family: str | None = None, capture_status: str = "imported",
                   import_id: int | None = None) -> None:
        if not is_sha256_hex(blob_sha256):
            raise StoreIntegrityError(f"非法 blob id：{blob_sha256!r}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO sources(source_id, blob_sha256, byte_length, media_type, "
                "locator, retrieved_at, published_at, published_at_provenance, "
                "time_evidence, document_subject, document_subject_basis, "
                "source_family, capture_status, import_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, blob_sha256, byte_length, media_type, locator, retrieved_at,
                 published_at, published_at_provenance,
                 json.dumps(time_evidence, ensure_ascii=False) if time_evidence else None,
                 document_subject, document_subject_basis,
                 source_family, capture_status, import_id),
            )

    def set_source_time_evidence(self, source_id: str, time_evidence: dict) -> None:
        """[已废弃于v2] 原地改写会被静默UPDATE；新代码用 append_time_evidence。

        保留仅为读取兼容：现实现改为追加一个新修订版本，不再覆盖旧值。
        """
        self.append_time_evidence(source_id, time_evidence)

    def add_claim(self, claim_id: str, source_id: str, *, locator_kind: str,
                  excerpt_start: int, excerpt_end: int, excerpt_sha256: str,
                  excerpt_text: str, interpretation: str, subject_scope: str,
                  interpretation_attempt: str | None = None,
                  locator_ref: str | None = None,
                  input_digest: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO claims(claim_id, source_id, locator_kind, locator_start, "
                "locator_end, locator_ref, excerpt_sha256, excerpt_text, interpretation, "
                "subject_scope, interpretation_attempt, input_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, source_id, locator_kind, excerpt_start, excerpt_end,
                 locator_ref, excerpt_sha256, excerpt_text, interpretation,
                 subject_scope, interpretation_attempt, input_digest),
            )

    def add_qualification(self, qual_id: str, claim_id: str, *, source_judgment: str,
                          identity_judgment: str, time_judgment: str,
                          independence_judgment: str, allowed_uses: list[str],
                          cannot_prove: list[str], review_attempt: str | None,
                          status: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO qualifications(qual_id, claim_id, source_judgment, "
                "identity_judgment, time_judgment, independence_judgment, allowed_uses, "
                "cannot_prove, review_attempt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (qual_id, claim_id, source_judgment, identity_judgment, time_judgment,
                 independence_judgment, json.dumps(allowed_uses, ensure_ascii=False),
                 json.dumps(cannot_prove, ensure_ascii=False), review_attempt, status),
            )

    def add_gap(self, gap_id: str, gap_type: str, affected_criteria: list[str], *,
                pipeline_fault: bool = False, investigation: str = "",
                unconfirmed: list[str] | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO gaps(gap_id, gap_type, affected_criteria, pipeline_fault, "
                "investigation, unconfirmed) VALUES (?,?,?,?,?,?)",
                (gap_id, gap_type, json.dumps(affected_criteria, ensure_ascii=False),
                 int(pipeline_fault), investigation,
                 json.dumps(unconfirmed or [], ensure_ascii=False)),
            )

    def add_criterion_result(self, result_id: str, criterion_id: str, dimension: str, *,
                             native_disposition: str | None, native_note: str | None,
                             product_status: str, evidence_refs: list[str],
                             gap_refs: list[str], qual_refs: list[str], rationale: str,
                             scope: str, rule_version: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO criterion_results(result_id, criterion_id, dimension, "
                "native_disposition, native_note, product_status, evidence_refs, "
                "gap_refs, qual_refs, rationale, scope, rule_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (result_id, criterion_id, dimension, native_disposition, native_note,
                 product_status, json.dumps(evidence_refs, ensure_ascii=False),
                 json.dumps(gap_refs, ensure_ascii=False),
                 json.dumps(qual_refs, ensure_ascii=False), rationale, scope,
                 rule_version),
            )

    # ---- 读取 ----

    def fetch_one(self, table: str, key_field: str, key_value: str) -> dict | None:
        allowed = {"sources", "claims", "qualifications", "gaps", "criterion_results",
                   "import_records"}
        if table not in allowed:
            raise ValueError(f"禁止查询表 {table}")
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_field} = ?", (key_value,)
        ).fetchone()
        return dict(row) if row else None

    def fetch_all(self, table: str) -> list[dict]:
        allowed = {"sources", "claims", "qualifications", "gaps", "criterion_results",
                   "import_records", "stages", "runs"}
        if table not in allowed:
            raise ValueError(f"禁止查询表 {table}")
        rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CaseStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def find_uncommitted_blobs(store: BlobStore, case: CaseStore) -> set[str]:
    """识别孤立工件：对象仓中存在、但没有任何 Source 引用的对象。

    这些是"落盘后未提交"的失败材料——保留不删，不得当作成功结果。
    """
    referenced = {row["blob_sha256"] for row in case.fetch_all("sources")}
    return store.list_objects() - referenced
