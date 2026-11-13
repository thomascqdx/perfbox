import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from perfbox.manifest import parse

SCRIPT = Path(__file__).parents[1] / "tools" / "stamp_manifest.py"

TEMPLATE = {
    "schema": 1,
    "id": "memscope",
    "title": "内存测试包 A/B 对比",
    "summary": "对比两个内存测试包",
    "domain": "memory",
    "version": "",
    "tag": "",
    "artifact": {"name": "", "kind": "exe", "size": 0, "sha256": ""},
    "commands": [{"id": "run", "title": "运行", "kind": "job",
                  "output": {"flag": "-o", "kind": "dir"},
                  "outputs": [{"glob": "*.txt", "role": "report"}]}],
}


def run(args, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=cwd)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "perfbox-tool.json").write_text(
        json.dumps(TEMPLATE, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "tool.exe").write_bytes(b"fake-exe")
    return tmp_path


def test_stamp_with_version(ws):
    r = run(["--version", "1.2.3", "--artifact", "tool.exe"], ws)
    assert r.returncode == 0, r.stderr
    out = ws / "dist" / "perfbox-tool.json"
    assert out.exists()
    assert "dist" in r.stdout and \
        hashlib.sha256(b"fake-exe").hexdigest() in r.stdout
    m = parse(out.read_bytes())  # 非模板校验必须通过
    assert m.version == "1.2.3" and m.tag == "v1.2.3"
    assert m.artifact.name == "tool.exe"
    assert m.artifact.size == 8
    assert m.artifact.sha256 == hashlib.sha256(b"fake-exe").hexdigest()
    # 其余内容原样保留
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["title"] == "内存测试包 A/B 对比"
    assert list(d.keys()) == list(TEMPLATE.keys())
    assert out.read_bytes().endswith(b"\n")


def test_version_from_dunder(ws):
    (ws / "__init__.py").write_text('__version__ = "1.2.3"\n',
                                    encoding="utf-8")
    r = run(["--version-from", "__init__.py", "--artifact", "tool.exe"],
            ws)
    assert r.returncode == 0, r.stderr
    m = parse((ws / "dist" / "perfbox-tool.json").read_bytes())
    assert m.version == "1.2.3"


def test_version_from_VERSION(ws):
    (ws / "jank_report.py").write_text("VERSION = '2.1.0'\n",
                                       encoding="utf-8")
    r = run(["--version-from", "jank_report.py", "--artifact", "tool.exe"],
            ws)
    assert r.returncode == 0, r.stderr
    m = parse((ws / "dist" / "perfbox-tool.json").read_bytes())
    assert m.version == "2.1.0"


def test_custom_template_and_out(ws):
    r = run(["--version", "1.0.0", "--artifact", "tool.exe",
             "--template", "perfbox-tool.json", "--out", "out/m.json"], ws)
    assert r.returncode == 0, r.stderr
    assert (ws / "out" / "m.json").exists()


def test_zip_artifact(ws):
    r = run(["--version", "1.0.0", "--artifact", "tool.exe",
             "--out", "d/m.json"], ws)
    assert r.returncode == 0, r.stderr
    d = json.loads((ws / "d" / "m.json").read_text(encoding="utf-8"))
    assert d["artifact"]["kind"] == "exe"  # kind 保留模板原值


def test_errors(ws):
    # 版本号格式错误
    r = run(["--version", "1.2", "--artifact", "tool.exe"], ws)
    assert r.returncode == 1 and r.stderr
    # 产物不存在
    r = run(["--version", "1.0.0", "--artifact", "nope.exe"], ws)
    assert r.returncode == 1 and r.stderr
    # version-from 文件不存在
    r = run(["--version-from", "nope.py", "--artifact", "tool.exe"], ws)
    assert r.returncode == 1 and r.stderr
    # version-from 找不到版本号
    (ws / "v.py").write_text("x = 1\n", encoding="utf-8")
    r = run(["--version-from", "v.py", "--artifact", "tool.exe"], ws)
    assert r.returncode == 1 and r.stderr
    # 模板不是合法 JSON
    (ws / "bad.json").write_text("[1]", encoding="utf-8")
    r = run(["--version", "1.0.0", "--artifact", "tool.exe",
             "--template", "bad.json"], ws)
    assert r.returncode == 1 and r.stderr
    # 模板没有 artifact 对象
    (ws / "noa.json").write_text('{"schema": 1}', encoding="utf-8")
    r = run(["--version", "1.0.0", "--artifact", "tool.exe",
             "--template", "noa.json"], ws)
    assert r.returncode == 1 and r.stderr
