"""任务台账：唯一认领、单调 token、失败留存。

合同（v3 计划 T03）：
- ``claim(task_key, worker_id, input_id)`` 原子认领，返回带单调 token 的认领记录；
  两个 worker 争抢同任务只有一个获得有效 token。
- 提交必须匹配有效 token **与** 输入身份；过期/被抢占 worker 提交被拒绝
  （fencing）。
- 失败留存：``record_failure`` 保留 attempt 历史；重试是**新建 attempt**，
  旧 attempt 永不删除。
- 外部任务状态：dispatch 已记录但无持久结果 → 保守 ``outcome_unknown``，
  不自动重发。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .contracts import EXTERNAL_TASK_STATES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN
        ('planned','claimed','dispatch_recorded','succeeded','failed','outcome_unknown')),
    current_token INTEGER,
    current_worker TEXT,
    input_id TEXT NOT NULL,
    output_ref TEXT,
    external_actions INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_no INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    token INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS token_sequence (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


class CommitRejected(RuntimeError):
    """提交被拒绝：token 无效、被抢占（fencing）或输入身份不符。"""


@dataclass(frozen=True)
class Claim:
    task_key: str
    worker_id: str
    token: int
    input_id: str
    attempt_no: int


class Journal:
    """SQLite 任务/阶段事务台账（与 CaseStore 可共用或独立数据库）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 认领 ----

    def ensure_task(self, task_key: str, input_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO tasks(task_key, state, input_id) "
                "VALUES (?, 'planned', ?)",
                (task_key, input_id),
            )

    def claim(self, task_key: str, worker_id: str, input_id: str) -> Claim:
        self.ensure_task(task_key, input_id)
        with self._conn:  # BEGIN ... COMMIT：update 带前置状态条件保证原子抢占
            row = self._conn.execute(
                "SELECT state, input_id FROM tasks WHERE task_key = ?", (task_key,)
            ).fetchone()
            if row["state"] not in ("planned", "failed"):
                raise CommitRejected(
                    f"任务 {task_key} 当前状态 {row['state']} 不可认领："
                    f"outcome_unknown/succeeded 需显式新任务或已验证幂等语义，"
                    f"不允许自动重发原请求"
                )
            if row["input_id"] != input_id:
                raise CommitRejected(
                    f"任务 {task_key} 输入身份不符：登记 {row['input_id']}，本次 {input_id}"
                )
            token_row = self._conn.execute(
                "UPDATE token_sequence SET value = value + 1 "
                "WHERE name = 'global' RETURNING value"
            ).fetchone()
            if token_row is None:
                self._conn.execute(
                    "INSERT INTO token_sequence(name, value) VALUES ('global', 1)"
                )
                token_value = 1
            else:
                token_value = int(token_row["value"])
            attempt_cur = self._conn.execute(
                "INSERT INTO task_attempts(task_key, token, worker_id, input_id, outcome) "
                "VALUES (?,?,?,?,'claimed')",
                (task_key, token_value, worker_id, input_id),
            )
            self._conn.execute(
                "UPDATE tasks SET state='claimed', current_token=?, current_worker=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_key=?",
                (token_value, worker_id, task_key),
            )
            return Claim(task_key, worker_id, token_value, input_id,
                         int(attempt_cur.lastrowid))

    # ---- 状态推进与提交 ----

    def _assert_claim(self, claim: Claim) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT state, current_token, current_worker, input_id FROM tasks "
            "WHERE task_key = ?",
            (claim.task_key,),
        ).fetchone()
        if row is None:
            raise CommitRejected(f"任务 {claim.task_key} 不存在")
        if row["current_token"] != claim.token or row["current_worker"] != claim.worker_id:
            raise CommitRejected(
                f"任务 {claim.task_key} token/worker 失配（fencing 拒绝）："
                f"期望 token={row['current_token']} worker={row['current_worker']}，"
                f"提交 token={claim.token} worker={claim.worker_id}"
            )
        if row["input_id"] != claim.input_id:
            raise CommitRejected(
                f"任务 {claim.task_key} 输入身份不符：登记 {row['input_id']}，"
                f"提交 {claim.input_id}"
            )
        return row

    def record_dispatch(self, claim: Claim) -> None:
        """外部动作已实际派发：先落账再行动。此状态后崩溃 → outcome_unknown。"""
        row = self._assert_claim(claim)
        if row["state"] != "claimed":
            raise CommitRejected(f"任务 {claim.task_key} 状态 {row['state']} 不可派发")
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET state='dispatch_recorded', "
                "external_actions = external_actions + 1, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_key=?",
                (claim.task_key,),
            )
            self._conn.execute(
                "UPDATE task_attempts SET outcome='dispatch_recorded', "
                "detail=COALESCE(detail,'') || 'dispatch;' WHERE attempt_no=?",
                (claim.attempt_no,),
            )

    def commit(self, claim: Claim, output_ref: str) -> None:
        """成功提交：匹配有效 token 与输入身份后写入结果引用。"""
        self._assert_claim(claim)
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET state='succeeded', output_ref=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_key=?",
                (output_ref, claim.task_key),
            )
            self._conn.execute(
                "UPDATE task_attempts SET outcome='succeeded' WHERE attempt_no=?",
                (claim.attempt_no,),
            )

    def record_failure(self, claim: Claim, detail: str, *,
                       outcome_unknown: bool = False) -> None:
        """失败留存：attempt 保留；外部动作已派发而无持久结果时保守 unknown。"""
        self._assert_claim(claim)
        state = "outcome_unknown" if outcome_unknown else "failed"
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET state=?, updated_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_key=?",
                (state, claim.task_key),
            )
            self._conn.execute(
                "UPDATE task_attempts SET outcome=?, detail=? WHERE attempt_no=?",
                (state, detail, claim.attempt_no),
            )

    # ---- 读取 ----

    def task_state(self, task_key: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_key = ?", (task_key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"任务 {task_key} 不存在")
        return dict(row)

    def attempts(self, task_key: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM task_attempts WHERE task_key = ? ORDER BY attempt_no",
            (task_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
