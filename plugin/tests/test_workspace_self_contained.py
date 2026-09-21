"""收口合同：异地包无旧目录/网络/子进程依赖，固定目录损坏即拒绝。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kth_hybrid import catalog
from kth_hybrid import census


def test_relocated_package_loads_catalog_without_legacy_access(tmp_path):
    package = Path(catalog.__file__).parent
    relocated = tmp_path / "checkout" / "plugin" / "src" / "kth_hybrid"
    shutil.copytree(package, relocated, ignore=shutil.ignore_patterns("__pycache__"))
    script = r"""
import json, os, sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
def guard(event, args):
    if event.startswith("socket.") or event == "subprocess.Popen":
        raise RuntimeError("禁止网络或子进程")
    if event in ("open", "os.listdir", "os.scandir", "sqlite3.connect") and args:
        value = args[0]
        if isinstance(value, (str, bytes, os.PathLike)):
            path = os.fsdecode(value).replace("\\", "/").lower()
            if path.startswith(("d:/ugit/sunny-skills/", "d:/t/")):
                raise RuntimeError("禁止旧目录访问")
            if path.endswith(".whl"):
                raise RuntimeError("正常应用不能读取wheel")
sys.addaudithook(guard)
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.proposal_requests import approved_catalog, validate_approved_catalog
from kth_hybrid.census import MANIFEST_DEFAULT
first = build_catalog_from_wheel()
value = approved_catalog()
assert first["dimensions"] == value["dimensions"]
assert first["totals"]["criteria"] == 180
assert len(value["dimensions"]["CRL"]["registry"]["criteria"]) == 13
assert validate_approved_catalog(value)
assert MANIFEST_DEFAULT == Path(sys.argv[1]).parents[1] / "provenance" / "manifest.v1.json"
print(json.dumps({"criteria": 180, "legacy_access": False, "subprocess": False}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", script, str(relocated.parent)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "criteria": 180, "legacy_access": False, "subprocess": False,
    }


def test_missing_catalog_fails_closed(tmp_path):
    with pytest.raises((FileNotFoundError, RuntimeError)):
        catalog.load_approved_catalog(tmp_path / "missing.json")


def test_damaged_catalog_fails_before_consumption(tmp_path):
    altered = tmp_path / "altered.json"
    altered.write_text('{"totals":{"criteria":180}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="身份不符"):
        catalog.load_approved_catalog(altered)


def test_catalog_claim_change_is_not_accepted(tmp_path):
    value = catalog.load_approved_catalog()
    value["dimensions"]["TRL"]["registry"]["criteria"][0]["text"] = "无依据的新规则"
    altered = tmp_path / "changed-rule.json"
    altered.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="身份不符"):
        catalog.load_approved_catalog(altered)


def test_loaded_catalog_is_not_mutable_shared_state():
    value = catalog.load_approved_catalog()
    value["dimensions"]["TRL"]["registry"]["criteria"].clear()
    assert len(catalog.load_approved_catalog()["dimensions"]["TRL"]["registry"]["criteria"]) == 25


@pytest.mark.parametrize("module", [catalog, census])
def test_shallow_install_location_does_not_break_module_import(module):
    filename = "D:/kth-site/kth_hybrid/" + Path(module.__file__).name
    namespace = {
        "__name__": module.__name__,
        "__package__": "kth_hybrid",
        "__file__": filename,
    }
    exec(compile(Path(module.__file__).read_text(encoding="utf-8"), filename, "exec"), namespace)
    if module is catalog:
        assert namespace["load_approved_catalog"](catalog.APPROVED_CATALOG)["totals"]["criteria"] == 180
