"""任务台账：唯一认领、单调 token、失败留存（v2：原子条件转换）。

R1.1 修复（验收 R1-04/R1-05）：
- 所有状态转换改为 **单条条件 UPDATE + 行数检查**，状态/token/worker/input
  条件写在 WHERE 中；写事务用 ``BEGIN IMMEDIATE`` 串行化，固定交错下双认领
  不可能同时成功。
- 终态不可变：succeeded/failed/outcome_unknown 不接受同 token 再次提交或
  改写；失败 attempt 不能被翻转成成功。
- 新增 ``takeover_stale_claim``：仅允许接管仍处 ``claimed`` 且**无外部动作**
  的失效认领（新 attempt、更大 token）；已派发/未知结果任务不允许静默接管。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN
        ('planned','claimed','dispatch_recorded','succeeded','failed','outcome_unknown')),
    current_token INTEGER,
    current_worker TEXT,
    owner_pid INTEGER,
    claimed_at TEXT,
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
    owner_pid INTEGER,
    claimed_at TEXT,
    input_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS token_sequence (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS takeover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    previous_owner_pid INTEGER,
    previous_claimed_at TEXT,
    stale_judgment TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

_CLAIMABLE_STATES = ("planned", "failed")
_ACTIVE_STATES = ("claimed", "dispatch_recorded")


class CommitRejected(RuntimeError):
    """提交被拒绝：token 无效、被抢占（fencing）、输入身份不符或终态不可变。"""


@dataclass(frozen=True)
class Claim:
    task_key: str
    worker_id: str
    token: int
    input_id: str
    attempt_no: int


class Journal:
    """SQLite 任务/阶段事务台账（v2：显式 BEGIN IMMEDIATE + 条件更新）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30,
                                     check_same_thread=False,
                                     isolation_level=None)  # 手动事务
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate_lease_columns()

    def _migrate_lease_columns(self) -> None:
        """为旧 Journal 补充只增字段的本地进程租约，不改写历史行。"""
        additions = {
            "tasks": {
                "owner_pid": "INTEGER",
                "claimed_at": "TEXT",
            },
            "task_attempts": {
                "owner_pid": "INTEGER",
                "claimed_at": "TEXT",
            },
            "takeover_events": {
                "previous_owner_pid": "INTEGER",
                "previous_claimed_at": "TEXT",
                "stale_judgment": "TEXT",
            },
        }
        for table, columns in additions.items():
            present = {row["name"] for row in self._conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in present:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _owner_pid_exited(owner_pid: object) -> bool:
        """仅在本机 PID 明确已退出时允许接管；任何不确定均视为仍活跃。"""
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) \
                or owner_pid <= 0:
            raise CommitRejected("claimed任务owner_pid缺失或不可核验，禁止自动接管")
        if owner_pid == os.getpid():
            return False
        if os.name == "nt":
            # os.kill(pid, 0) 在 Windows 不可靠地对已退出 PID 返回成功；用
            # 标准库 ctypes 查询进程句柄和退出码，PID 重用时仍会保守视为活跃。
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            error_invalid_parameter = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, owner_pid)
            if not handle:
                return ctypes.get_last_error() == error_invalid_parameter
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value != still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            # Windows PID 重用、权限或平台错误均不能证明原任务已退出。
            return False
        return False

    # ---- 内部：串行写事务与单调 token ----

    def _write_txn(self):
        return _WriteTxn(self._conn)

    def _next_token(self) -> int:
        row = self._conn.execute(
            "UPDATE token_sequence SET value = value + 1 WHERE name='global' "
            "RETURNING value"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO token_sequence(name, value) VALUES ('global', 1)")
            return 1
        return int(row["value"])

    # ---- 认领 ----

    def ensure_task(self, task_key: str, input_id: str) -> None:
        with self._write_txn():
            self._conn.execute(
                "INSERT OR IGNORE INTO tasks(task_key, state, input_id) "
                "VALUES (?, 'planned', ?)",
                (task_key, input_id),
            )

    def claim(self, task_key: str, worker_id: str, input_id: str) -> Claim:
        """原子认领：状态/input 条件在 UPDATE 的 WHERE 中，行数必须为 1。"""
        with self._write_txn():
            row = self._conn.execute(
                "SELECT state, input_id, external_actions FROM tasks WHERE task_key=?",
                (task_key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO tasks(task_key, state, input_id) "
                    "VALUES (?, 'planned', ?)", (task_key, input_id))
                row = {
                    "state": "planned",
                    "input_id": input_id,
                    "external_actions": 0,
                }
            if row["state"] not in _CLAIMABLE_STATES:
                raise CommitRejected(
                    f"任务 {task_key} 当前状态 {row['state']} 不可认领："
                    f"outcome_unknown/succeeded 需显式新任务或已验证幂等语义，"
                    f"不允许自动重发原请求"
                )
            if row["external_actions"] != 0:
                raise CommitRejected(
                    f"任务 {task_key} 已记录外部动作，禁止重新认领或重派")
            if row["input_id"] != input_id:
                raise CommitRejected(
                    f"任务 {task_key} 输入身份不符：登记 {row['input_id']}，"
                    f"本次 {input_id}"
                )
            token = self._next_token()
            owner_pid = os.getpid()
            cur = self._conn.execute(
                "UPDATE tasks SET state='claimed', current_token=?, current_worker=?, "
                "owner_pid=?, claimed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND input_id=? AND state IN ('planned','failed')",
                (token, worker_id, owner_pid, task_key, input_id),
            )
            if cur.rowcount != 1:
                raise CommitRejected(
                    f"任务 {task_key} 认领条件更新行数 {cur.rowcount}≠1（并发抢占）"
                )
            attempt = self._conn.execute(
                "INSERT INTO task_attempts(task_key, token, worker_id, owner_pid, "
                "claimed_at, input_id, outcome) VALUES (?,?,?,?,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,'claimed')",
                (task_key, token, worker_id, owner_pid, input_id),
            )
            return Claim(task_key, worker_id, token, input_id,
                         int(attempt.lastrowid))

    # ---- 状态推进与提交（原子条件转换）----

    def _reject_reason(self, task_key: str, claim: Claim, action: str) -> CommitRejected:
        """条件更新失败时区分原因：输入身份 / fencing / 终态不可变。"""
        row = self._conn.execute(
            "SELECT state, current_token, current_worker, input_id FROM tasks "
            "WHERE task_key=?", (task_key,),
        ).fetchone()
        if row is None:
            return CommitRejected(f"任务 {task_key} 不存在（{action}）")
        if row["input_id"] != claim.input_id:
            return CommitRejected(
                f"任务 {task_key} {action}输入身份不符：登记 {row['input_id']}，"
                f"提交 {claim.input_id}"
            )
        if row["state"] in ("succeeded", "failed", "outcome_unknown"):
            return CommitRejected(
                f"任务 {task_key} {action}被拒：终态 {row['state']} 不可变"
            )
        return CommitRejected(
            f"任务 {task_key} {action}被拒（fencing 失配）：期望 token="
            f"{row['current_token']} worker={row['current_worker']}，"
            f"提交 token={claim.token} worker={claim.worker_id}"
        )

    def record_dispatch(self, claim: Claim) -> None:
        """外部动作已实际派发：先落账再行动。此状态后崩溃 → 保守 unknown。"""
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE tasks SET state='dispatch_recorded', "
                "external_actions = external_actions + 1, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND current_token=? AND current_worker=? "
                "AND input_id=? AND state='claimed'",
                (claim.task_key, claim.token, claim.worker_id, claim.input_id),
            )
            if cur.rowcount != 1:
                raise self._reject_reason(claim.task_key, claim, "派发")
            self._conn.execute(
                "UPDATE task_attempts SET outcome='dispatch_recorded', "
                "detail=COALESCE(detail,'') || 'dispatch;' WHERE attempt_no=? "
                "AND outcome='claimed'",
                (claim.attempt_no,),
            )

    def commit(self, claim: Claim, output_ref: str) -> None:
        """成功提交：条件更新（终态不可变），拒绝同 token 二次提交与迟到覆盖。"""
        with self._write_txn():
            cur = self._conn.execute(
                "UPDATE tasks SET state='succeeded', output_ref=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND current_token=? AND current_worker=? "
                "AND input_id=? AND state IN ('claimed','dispatch_recorded')",
                (output_ref, claim.task_key, claim.token, claim.worker_id,
                 claim.input_id),
            )
            if cur.rowcount != 1:
                raise self._reject_reason(claim.task_key, claim, "提交")
            self._conn.execute(
                "UPDATE task_attempts SET outcome='succeeded' WHERE attempt_no=? "
                "AND outcome IN ('claimed','dispatch_recorded')",
                (claim.attempt_no,),
            )

    def record_failure(self, claim: Claim, detail: str, *,
                       outcome_unknown: bool = False) -> None:
        """失败留存：attempt 保留；外部动作已派发而无持久结果时保守 unknown。"""
        state_value = "outcome_unknown" if outcome_unknown else "failed"
        with self._write_txn():
            cur = self._conn.execute(
                f"UPDATE tasks SET state='{state_value}', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND current_token=? AND current_worker=? "
                "AND input_id=? AND state IN ('claimed','dispatch_recorded')",
                (claim.task_key, claim.token, claim.worker_id, claim.input_id),
            )
            if cur.rowcount != 1:
                raise self._reject_reason(claim.task_key, claim, "失败登记")
            self._conn.execute(
                "UPDATE task_attempts SET outcome=?, detail=? WHERE attempt_no=? "
                "AND outcome IN ('claimed','dispatch_recorded')",
                (state_value, detail, claim.attempt_no),
            )

    # ---- 失效认领接管（R1-05）----

    def takeover_stale_claim(self, task_key: str, new_worker: str,
                             input_id: str, *, evidence: str) -> Claim:
        """受控接管：仅限原本机进程已退出的本地 ``claimed`` 任务。

        仅有字符串 evidence 不能证明失效。旧任务必须有可核验的本机 owner_pid
        与 claimed_at，且平台本机进程查询明确报告 PID 已退出。已派发或结果未知
        的任务始终不允许静默接管。
        """
        if not evidence or not evidence.strip():
            raise CommitRejected("接管必须携带失效证据（evidence）")
        with self._write_txn():
            row = self._conn.execute(
                "SELECT state, current_worker, external_actions, input_id, "
                "owner_pid, claimed_at "
                "FROM tasks WHERE task_key=?", (task_key,),
            ).fetchone()
            if row is None:
                raise CommitRejected(f"任务 {task_key} 不存在")
            if row["input_id"] != input_id:
                raise CommitRejected(
                    f"任务 {task_key} 接管输入身份不符：{row['input_id']} vs {input_id}"
                )
            if row["state"] != "claimed" or row["external_actions"] != 0:
                raise CommitRejected(
                    f"任务 {task_key} 状态 {row['state']}/外部动作 "
                    f"{row['external_actions']}：已派发或未知结果的任务不允许"
                    f"静默接管（先按保守恢复流程处理）"
                )
            if not isinstance(row["claimed_at"], str) or not row["claimed_at"]:
                raise CommitRejected(
                    f"任务 {task_key} claimed_at缺失或不可核验，禁止自动接管")
            if not self._owner_pid_exited(row["owner_pid"]):
                raise CommitRejected(
                    f"任务 {task_key} owner_pid={row['owner_pid']}仍存活或"
                    "无法确认已退出，禁止自动接管")
            token = self._next_token()
            owner_pid = os.getpid()
            cur = self._conn.execute(
                "UPDATE tasks SET state='claimed', current_token=?, current_worker=?, "
                "owner_pid=?, claimed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND input_id=? AND state='claimed' "
                "AND external_actions=0",
                (token, new_worker, owner_pid, task_key, input_id),
            )
            if cur.rowcount != 1:
                raise CommitRejected(f"任务 {task_key} 接管条件更新失败")
            attempt = self._conn.execute(
                "INSERT INTO task_attempts(task_key, token, worker_id, owner_pid, "
                "claimed_at, input_id, outcome, detail) VALUES (?,?,?,?,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,'claimed',?)",
                (task_key, token, new_worker, owner_pid, input_id,
                 f"takeover: {evidence}"),
            )
            self._conn.execute(
                "INSERT INTO takeover_events(task_key, attempt_no, worker_id, "
                "evidence, previous_owner_pid, previous_claimed_at, "
                "stale_judgment) VALUES (?,?,?,?,?,?,?)",
                (task_key, int(attempt.lastrowid), new_worker, evidence,
                 row["owner_pid"], row["claimed_at"],
                 "owner_pid_exit_confirmed"),
            )
            return Claim(task_key, new_worker, token, input_id,
                         int(attempt.lastrowid))

    def takeover_events(self, task_key: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM takeover_events WHERE task_key=? ORDER BY id",
            (task_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_recovered_unknown(self, task_key: str) -> None:
        """恢复期保守解释：dispatch 已落账但无持久结果 → outcome_unknown。"""
        self.recover_dispatched_unknown(task_key=task_key)

    def recover_dispatched_unknown(self, *, task_key: str | None = None) -> list[dict]:
        """将恢复时未封账的已派发任务保守冻结为 unknown，并返回新冻结项。"""
        with self._write_txn():
            where = "state='dispatch_recorded'"
            parameters: tuple = ()
            if task_key is not None:
                where += " AND task_key=?"
                parameters = (task_key,)
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE " + where + " ORDER BY task_key",
                parameters).fetchall()
            if rows:
                task_keys = [row["task_key"] for row in rows]
                self._conn.execute(
                    "UPDATE tasks SET state='outcome_unknown', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE " + where,
                    parameters)
                placeholders = ",".join("?" for _ in task_keys)
                self._conn.execute(
                    "UPDATE task_attempts SET outcome='outcome_unknown', "
                    "detail=COALESCE(detail,'') || 'recovered:dispatch_recorded;' "
                    f"WHERE task_key IN ({placeholders}) "
                    "AND outcome='dispatch_recorded'",
                    task_keys,
                )
            return [dict(row) for row in rows]

    def unresolved_external_actions(self) -> list[dict]:
        """返回必须人工确认的 unknown 外部动作；不提供自动接管入口。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE state='outcome_unknown' "
            "AND external_actions>0 ORDER BY task_key").fetchall()
        return [dict(row) for row in rows]

    def complete_existing_local_task(
            self, task_key: str, *, input_id: str, output_ref: str,
            worker_id: str, evidence: str,
            allow_failed_recovery: bool = False) -> dict:
        """核验已有本地结果后安全封账，不适用于任何已派发任务。

        ``claimed`` 仅在 ``external_actions=0`` 时以新token接管并留存事件；
        ``failed`` 必须由调用方显式开启受控恢复。已成功任务只做精确身份核验。
        """
        if not all(isinstance(value, str) and value.strip() for value in (
                task_key, input_id, output_ref, worker_id, evidence)):
            raise CommitRejected("本地任务封账必须提供完整身份、输出和恢复证据")
        state = self.task_state(task_key)
        if state["input_id"] != input_id:
            raise CommitRejected(
                f"本地任务 {task_key} 输入不一致：{state['input_id']} vs {input_id}")
        if state["state"] == "succeeded":
            if state["output_ref"] != output_ref:
                raise CommitRejected(
                    f"本地任务 {task_key} 已成功但输出不一致："
                    f"{state['output_ref']} vs {output_ref}")
            return state
        if state["state"] in {"dispatch_recorded", "outcome_unknown"} \
                or state["external_actions"] != 0:
            raise CommitRejected(
                f"本地任务 {task_key} 已派发/unknown或存在外部动作，绝不自动接管")
        if state["state"] == "failed" and not allow_failed_recovery:
            raise CommitRejected(
                f"本地任务 {task_key} 为failed，必须显式授权受控恢复")
        if state["state"] == "claimed":
            claim = self.takeover_stale_claim(
                task_key, worker_id, input_id, evidence=evidence)
        elif state["state"] in {"planned", "failed"}:
            claim = self.claim(task_key, worker_id, input_id)
        else:
            raise CommitRejected(
                f"本地任务 {task_key} 状态 {state['state']} 不可封账")
        self.commit(claim, output_ref)
        return self.task_state(task_key)

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


class _WriteTxn:
    """BEGIN IMMEDIATE 写事务上下文：串行化写者，异常回滚。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False
