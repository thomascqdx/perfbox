"""updates.py 的单元测试：缓存 TTL、incompatible/error 记录、原子写。"""

import json
import time

import pytest

from perfbox import store, updates
from perfbox.sources import DirSource

from helpers import entry_of, publish


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    return home, src


def test_read_cache_missing_and_corrupt(env):
    home, _ = env
    home.mkdir(parents=True)
    assert updates.read_cache(home) == {"checked": 0, "tools": {}}
    (home / "update-cache.json").write_text("not json{", encoding="utf-8")
    assert updates.read_cache(home) == {"checked": 0, "tools": {}}
    (home / "update-cache.json").write_text('{"checked": "x"}', encoding="utf-8")
    assert updates.read_cache(home) == {"checked": 0, "tools": {}}


def test_check_all_records_latest(env):
    home, src = env
    publish(src, "memscope", "1.2.3")
    cache = updates.check_all([entry_of()], [DirSource(src)], home)
    e = cache["tools"]["memscope"]
    assert e == {"latest": "1.2.3", "error": None, "incompatible": None}
    assert cache["checked"] > 0
    # 缓存已落盘
    assert updates.read_cache(home)["tools"]["memscope"]["latest"] == "1.2.3"


def test_ttl_skip_and_force(env):
    home, src = env
    publish(src, "memscope", "1.0.0")
    t = [1000.0]
    now = lambda: t[0]
    c1 = updates.check_all([entry_of()], [DirSource(src)], home, now=now)
    # 源里换成 2.0.0，但未过 TTL、未 force：返回旧缓存
    publish(src, "memscope", "2.0.0", body=b"new")
    t[0] += updates.TTL - 1
    c2 = updates.check_all([entry_of()], [DirSource(src)], home, now=now)
    assert c2["tools"]["memscope"]["latest"] == "1.0.0"
    assert c2["checked"] == c1["checked"]
    c3 = updates.check_all([entry_of()], [DirSource(src)], home, now=now,
                           force=True)
    assert c3["tools"]["memscope"]["latest"] == "2.0.0"
    # 过了 TTL 自动重查
    t[0] += updates.TTL + 1
    c4 = updates.check_all([entry_of()], [DirSource(src)], home, now=now)
    assert c4["tools"]["memscope"]["latest"] == "2.0.0"


def test_incompatible_manifest(env):
    home, src = env
    publish(src, "memscope", "9.9.9",
            manifest_extra={"schema": 2, "min_perfbox": "9.9"})
    cache = updates.check_all([entry_of()], [DirSource(src)], home)
    e = cache["tools"]["memscope"]
    assert e["latest"] is None
    assert e["incompatible"]
    assert e["error"] is None


def test_unreachable_source_records_error(env):
    home, _ = env
    cache = updates.check_all([entry_of()],
                              [DirSource(env[0] / "nonexistent-src")], home)
    e = cache["tools"]["memscope"]
    assert e["latest"] is None
    assert e["error"]


def test_tool_missing_in_source(env):
    home, src = env
    src.mkdir(exist_ok=True)
    cache = updates.check_all([entry_of()], [DirSource(src)], home)
    e = cache["tools"]["memscope"]
    assert e["latest"] is None
    assert e["error"]


def test_check_all_never_raises_on_source_errors(env):
    home, _ = env

    class BoomSource:
        def latest_manifest(self, *a):
            from perfbox.sources import SourceError
            raise SourceError("网络炸了")

        def fetch_artifact(self, *a, **k):
            raise AssertionError("不应走到下载")

    cache = updates.check_all([entry_of()], [BoomSource()], home)
    assert "网络炸了" in cache["tools"]["memscope"]["error"]
