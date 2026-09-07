"""T02：执行边界测试——离线、零凭证、限制写入与子进程豁免。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kth_hybrid import catalog


def test_network_is_blocked():
    import socket

    with pytest.raises(RuntimeError, match="网络"):
        socket.socket()


def test_unexempted_subprocess_is_blocked():
    # 未带 -I 隔离标志的解释器子进程被拒。
    with pytest.raises(RuntimeError, match="隔离模式"):
        subprocess.run([sys.executable, "-c", "pass"])


def test_isolated_interpreter_child_is_allowed():
    result = subprocess.run(
        [sys.executable, "-I", "-c", "print(7*6)"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0 and result.stdout.strip() == "42"


def test_foreign_executable_is_blocked_even_inside_probe():
    with catalog.isolated_wheel_probe():
        with pytest.raises(RuntimeError, match="受控解释器"):
            executable = "C:/Windows/System32/cmd.exe"
            subprocess.run([executable, "/c", "echo hi"], capture_output=True, timeout=30)


def test_write_outside_allowed_roots_is_blocked(tmp_path):
    target = os.path.expanduser("~/kth_boundary_forbidden_test.txt")
    with pytest.raises(RuntimeError, match="越界写入"):
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
    assert not os.path.exists(target)


def test_write_inside_tmp_is_allowed(tmp_path):
    target = tmp_path / "allowed.txt"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("ok")
    assert target.read_text(encoding="utf-8") == "ok"


def test_credential_files_are_not_readable(tmp_path):
    credential = tmp_path / "kth-env"
    # 在允许根内造出一个标记文件（写入允许根不违规），加载读取必须被拒。
    credential.write_bytes(b"SECRET=do-not-print")
    with pytest.raises(RuntimeError, match="凭证"):
        with open(credential, "r", encoding="utf-8") as handle:
            handle.read()


def test_sqlite_outside_allowed_roots_is_blocked():
    outside = Path(__file__).parent / "boundary-outside.sqlite"
    with pytest.raises(RuntimeError, match="sqlite"):
        sqlite3.connect(str(outside))
    assert not outside.exists()


def test_sqlite_inside_allowed_roots_works(tmp_path):
    connection = sqlite3.connect(str(tmp_path / "case.sqlite"))
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    assert connection.execute("SELECT x FROM t").fetchone()[0] == 1
    connection.close()
