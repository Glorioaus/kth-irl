"""从当前插件自身加载 KTH 本地 CLI。"""

from __future__ import annotations

from pathlib import Path
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = (PLUGIN_ROOT / "src").resolve()
sys.path.insert(0, str(PLUGIN_SRC))

from kth_hybrid import cli  # noqa: E402


loaded_cli = Path(cli.__file__).resolve()
if not loaded_cli.is_relative_to(PLUGIN_SRC):
    raise RuntimeError(f"拒绝加载插件外CLI实现：{loaded_cli}")


if __name__ == "__main__":
    raise SystemExit(cli.main())
