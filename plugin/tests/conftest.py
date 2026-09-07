"""测试入口：受控环境与执行边界（离线、零凭证、限制写入）。

执行边界为**尽力而为的 CPython 审计钩子**：拦截 socket、无豁免的 subprocess、
Python 层越界写入、凭证文件读取与越界 sqlite3.connect。注意 sqlite3 的原生
VFS 文件 IO 不经过 ``open`` 审计事件，因此 sqlite 数据库必须由被测代码在
允许根内打开（``sqlite3.connect`` 事件本身会被检查）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 允许写入的根：pytest 临时目录、pytest 缓存、环境变量指定的 Case 根。
_CASE_ROOT = os.environ.get("KTH_CASE_ROOT")
_ALLOWED_ROOTS = [str(Path(tempfile.gettempdir()).resolve()), str(Path(".pytest_cache").resolve())]
if _CASE_ROOT:
    _ALLOWED_ROOTS.append(str(Path(_CASE_ROOT).resolve()))

_CREDENTIAL_PATTERNS = ("kth-env", "kth_env", ".env")


_WINDOWS_DEVICES = {"nul", "con", "prn", "aux", *{f"com{i}" for i in range(1, 10)},
                    *{f"lpt{i}" for i in range(1, 10)}}


def _path_allowed(value) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return True
    try:
        raw = os.fsdecode(value)
    except (OSError, ValueError):
        return False
    if raw.startswith("\\\\.\\"):  # Windows 设备路径（如 \\.\nul）
        return True
    for prefix in ("\\\\?\\", "\\\\?\\UNC\\"):  # 扩展长度路径前缀
        if raw.startswith(prefix):
            raw = raw[len(prefix):] or "NUL"
            break
    try:
        if Path(raw).name.lower() in _WINDOWS_DEVICES:
            return True
    except (OSError, ValueError):
        return False
    try:
        resolved = Path(raw).resolve()
    except (OSError, ValueError):
        return False
    for root in _ALLOWED_ROOTS:
        try:
            if resolved.is_relative_to(Path(root)):
                return True
        except (OSError, ValueError):
            continue
    return False


def _is_credential_path(value) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    try:
        name = Path(os.fsdecode(value)).name.lower()
    except (OSError, ValueError):
        return True
    return any(pattern in name for pattern in _CREDENTIAL_PATTERNS)


def _install_guard() -> None:
    sys.dont_write_bytecode = True  # 阻止 src 树产生 __pycache__ 越界写入

    def guard(event, args):
        if event.startswith("socket."):
            raise RuntimeError(f"执行边界：离线测试禁止网络（{event}）")
        if event == "subprocess.Popen":
            from kth_hybrid import catalog

            if not catalog.probe_active():
                raise RuntimeError("执行边界：禁止无豁免子进程（仅批准 wheel 隔离探针放行）")
            # 事件参数：(executable, args, cwd, env)；Windows 下 executable 可能为 None。
            program = args[0] if args and args[0] else None
            if program is None and len(args) > 1:
                argv = args[1]
                if isinstance(argv, (list, tuple)) and argv:
                    program = argv[0]
                elif isinstance(argv, str):
                    program = argv.split(None, 1)[0].strip('"')
            program = os.fsdecode(program) if program else ""
            if program.lower() != sys.executable.lower():
                raise RuntimeError(f"执行边界：仅允许受控解释器，拒绝 {program}")
            return
        if event == "sqlite3.connect":
            target = args[0] if args else ""
            if isinstance(target, str) and not target.startswith(("file:", ":memory:")):
                if not _path_allowed(target):
                    raise RuntimeError(f"执行边界：sqlite 数据库越界（{target}）")
            return
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            )
            if writing:
                if not _path_allowed(args[0]):
                    raise RuntimeError(f"执行边界：越界写入 {args[0]!r}")
            elif _is_credential_path(args[0]):
                # 凭证只拦"加载进进程"的读取；不打印其内容。
                raise RuntimeError(f"执行边界：禁止读取凭证文件 {args[0]!r}")
            return
        for event_name in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"):
            if event == event_name:
                if event_name == "os.mkdir" and Path(os.fsdecode(args[0])).is_dir():
                    return  # mkdir(exist_ok=True) 对已存在目录是无操作
                if not _path_allowed(args[0]):
                    raise RuntimeError(f"执行边界：越界文件操作 {event} {args[0]!r}")
        if event in ("os.rename", "os.replace") and not (
            _path_allowed(args[0]) and _path_allowed(args[1])
        ):
            raise RuntimeError(f"执行边界：越界替换 {event} {args[0]!r} -> {args[1]!r}")

    sys.addaudithook(guard)


_install_guard()


@pytest.fixture(autouse=True)
def _case_root_default(tmp_path):
    """每个测试默认拥有一个受控临时工作目录。"""
    return tmp_path
