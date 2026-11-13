import json
import subprocess

import pytest

import perfbox
from perfbox import cli

from helpers import make_manifest, publish


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    monkeypatch.setenv("PERFBOX_SOURCE", str(src))
    return home, src


def test_version(capsys):
    assert cli.main(["-V"]) == 0
    assert capsys.readouterr().out.strip() == f"perfbox {perfbox.__version__}"


def test_no_args_runs_ui(monkeypatch):
    calls = []
    monkeypatch.setattr("perfbox.server.serve",
                        lambda **kw: calls.append(kw) or 0)
    assert cli.main([]) == 0
    assert calls == [{"port": 0, "open_browser": True}]


def test_ui_subcommand(monkeypatch):
    calls = []
    monkeypatch.setattr("perfbox.server.serve",
                        lambda **kw: calls.append(kw) or 0)
    assert cli.main(["ui"]) == 0
    assert calls == [{"port": 0, "open_browser": True}]
    calls.clear()
    assert cli.main(["ui", "--port", "8899", "--no-browser"]) == 0
    assert calls == [{"port": 8899, "open_browser": False}]


def test_list_uninstalled(env, capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "memscope" in out and "perfetto-trim" in out
    assert "未安装" in out


def test_list_check_failure(env, capsys, monkeypatch):
    monkeypatch.setenv("PERFBOX_SOURCE", str(env[1] / "nonexistent"))
    assert cli.main(["list"]) == 0
    assert "检查失败" in capsys.readouterr().out


def test_install(env, capsys):
    home, src = env
    publish(src, "memscope", "1.0.0", body=b"E")
    assert cli.main(["install", "memscope"]) == 0
    assert "已安装 memscope 1.0.0" in capsys.readouterr().out
    assert cli.main(["install", "memscope"]) == 0
    assert "memscope 1.0.0 已是最新" in capsys.readouterr().out


def test_install_unknown_tool(env, capsys):
    assert cli.main(["install", "nope"]) == 1
    err = capsys.readouterr().err
    assert "perfbox: 错误：" in err and "未知工具" in err


def test_list_shows_update(env, capsys):
    home, src = env
    publish(src, "memscope", "1.0.0")
    cli.main(["install", "memscope"])
    capsys.readouterr()
    publish(src, "memscope", "1.1.0")
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "1.0.0" in out and "1.1.0" in out


def test_update(env, capsys):
    home, src = env
    publish(src, "memscope", "1.0.0")
    cli.main(["install", "memscope"])
    publish(src, "memscope", "1.1.0")
    capsys.readouterr()
    assert cli.main(["update", "memscope"]) == 0
    out = capsys.readouterr().out
    assert "memscope" in out and "1.1.0" in out
    assert cli.main(["update", "memscope"]) == 0
    assert "已是最新" in capsys.readouterr().out


def test_update_all(env, capsys):
    home, src = env
    publish(src, "memscope", "1.0.0")
    cli.main(["install", "memscope"])
    publish(src, "memscope", "1.1.0")
    capsys.readouterr()
    assert cli.main(["update", "--all"]) == 0
    assert "memscope" in capsys.readouterr().out


def test_rollback(env, capsys):
    home, src = env
    publish(src, "memscope", "1.0.0", body=b"v1")
    cli.main(["install", "memscope"])
    publish(src, "memscope", "1.1.0", body=b"v2")
    cli.main(["update", "memscope"])
    capsys.readouterr()
    assert cli.main(["rollback", "memscope"]) == 0
    assert "1.0.0" in capsys.readouterr().out


def test_run_installs_then_passthrough(env, capsys, monkeypatch):
    home, src = env
    publish(src, "memscope", "1.0.0", body=b"E")
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda argv: calls.append(argv) or 3)
    rc = cli.main(["run", "memscope", "--", "-V", "a b"])
    assert rc == 3
    inst_entry = calls[0][0]
    assert inst_entry.endswith("memscope-1.0.0.exe")
    assert calls[0][1:] == ["-V", "a b"]


def test_run_no_source_access_when_installed(env, capsys, monkeypatch):
    home, src = env
    publish(src, "memscope", "1.0.0", body=b"E")
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda argv: calls.append(argv) or 0)
    assert cli.main(["run", "memscope", "-V"]) == 0
    # 已装版本运行不访问任何源
    monkeypatch.setenv("PERFBOX_SOURCE", str(src / "nonexistent"))
    assert cli.main(["run", "memscope", "-V"]) == 0
    assert len(calls) == 2


def test_manifest_check(env, capsys, tmp_path):
    m = tmp_path / "perfbox-tool.json"
    m.write_text(json.dumps(make_manifest("memscope", "1.0.0", size=3,
                                          sha256="a" * 64)),
                 encoding="utf-8")
    assert cli.main(["manifest", "check", str(m)]) == 0
    assert "OK: memscope 1.0.0，1 个命令" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 1}', encoding="utf-8")
    assert cli.main(["manifest", "check", str(bad)]) == 1
    assert "perfbox: 错误：" in capsys.readouterr().err


def test_manifest_check_template(env, capsys, tmp_path):
    m = tmp_path / "tpl.json"
    tpl = make_manifest("memscope", "", size=0, sha256="", name="", tag="")
    m.write_text(json.dumps(tpl), encoding="utf-8")
    assert cli.main(["manifest", "check", str(m)]) == 1
    capsys.readouterr()
    assert cli.main(["manifest", "check", "--template", str(m)]) == 0
    assert "OK: memscope" in capsys.readouterr().out
