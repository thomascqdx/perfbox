"""updates.py 的单元测试：缓存 TTL、incompatible/error 记录、原子写、宿主更新检查。"""

import http.server
import json
import socket
import threading
import time

import pytest

from perfbox import store, updates
from perfbox.sources import DirSource

from helpers import entry_of, publish


def _no_host(**kw):
    return {"latest": None, "url": "u", "error": None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    # 测试不联网：宿主检查换成桩，真实 check_host 由 TestCheckHost 覆盖
    monkeypatch.setattr(updates, "check_host", _no_host)
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


def test_check_all_includes_host_result(env, monkeypatch):
    home, src = env
    publish(src, "memscope", "1.0.0")
    calls = []
    monkeypatch.setattr(
        updates, "check_host",
        lambda **kw: calls.append(kw) or
        {"latest": "9.9.9", "url": "http://x/latest", "error": None})
    c = updates.check_all([entry_of()], [DirSource(src)], home,
                          proxy="http://p:1")
    assert c["host"] == {"latest": "9.9.9", "url": "http://x/latest",
                         "error": None}
    assert calls == [{"proxy": "http://p:1"}]
    # TTL 内重查走缓存，host 也在缓存里
    c2 = updates.check_all([entry_of()], [DirSource(src)], home)
    assert len(calls) == 1
    assert c2["host"]["latest"] == "9.9.9"
    updates.check_all([entry_of()], [DirSource(src)], home, force=True)
    assert len(calls) == 2


class _HostHandler(http.server.BaseHTTPRequestHandler):
    status = 302
    location = None

    def do_GET(self):
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def httpd():
    _HostHandler.status = 302
    _HostHandler.location = None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HostHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


class TestCheckHost:
    def _url(self, srv):
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_302_to_tag(self, httpd):
        _HostHandler.location = "/thomascqdx/perfbox/releases/tag/v0.5.0"
        r = updates.check_host(base_url=self._url(httpd))
        assert r == {
            "latest": "0.5.0", "error": None,
            "url": f"{self._url(httpd)}/thomascqdx/perfbox/releases/latest"}

    def test_404(self, httpd):
        _HostHandler.status = 404
        r = updates.check_host(base_url=self._url(httpd))
        assert r["latest"] is None and r["error"] is None

    def test_garbage_location(self, httpd):
        _HostHandler.location = "/somewhere/else"
        r = updates.check_host(base_url=self._url(httpd))
        assert r["latest"] is None and r["error"] is None

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = updates.check_host(base_url=f"http://127.0.0.1:{port}",
                               timeout=3)
        assert r["latest"] is None and r["error"]

    def test_never_raises_on_bad_status(self, httpd):
        _HostHandler.status = 500
        r = updates.check_host(base_url=self._url(httpd))
        assert r["latest"] is None
