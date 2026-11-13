import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from perfbox import sources
from perfbox.catalog import CatalogEntry
from perfbox.manifest import parse
from perfbox.paths import Config
from perfbox.sources import DirSource, GitHubSource, SourceError, configured

from helpers import entry_of, make_manifest, publish


# ------------------------------------------------------------- DirSource

class TestDirSource:
    def test_latest_manifest_picks_numeric_max(self, tmp_path):
        publish(tmp_path, "memscope", "0.9.0")
        publish(tmp_path, "memscope", "0.10.0")
        publish(tmp_path, "memscope", "0.9.5")
        (tmp_path / "memscope" / "latest").mkdir()  # 非版本号目录忽略
        (tmp_path / "memscope" / "9.9.9").mkdir()   # 无清单的目录忽略
        raw = DirSource(tmp_path).latest_manifest(entry_of())
        assert parse(raw).version == "0.10.0"

    def test_missing_tool_returns_none(self, tmp_path):
        assert DirSource(tmp_path).latest_manifest(entry_of()) is None

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(SourceError):
            DirSource(tmp_path / "nope").latest_manifest(entry_of())

    def test_fetch_artifact_with_progress(self, tmp_path):
        body = b"x" * 5000
        publish(tmp_path, "memscope", "1.0.0", body=body)
        src = DirSource(tmp_path)
        m = parse(src.latest_manifest(entry_of()))
        dest = tmp_path / "out.part"
        calls = []
        src.fetch_artifact(
            entry_of(), m, dest, lambda d, t: calls.append((d, t)))
        assert dest.read_bytes() == body
        assert calls[-1] == (5000, 5000)

    def test_fetch_missing_artifact(self, tmp_path):
        publish(tmp_path, "memscope", "1.0.0")
        m = parse(json.dumps(make_manifest(
            "memscope", "9.9.9", name="memscope-9.9.9.exe", size=3,
            sha256="a" * 64)))
        with pytest.raises(SourceError):
            DirSource(tmp_path).fetch_artifact(
                entry_of(), m, tmp_path / "o.part", None)

    def test_label_is_root(self, tmp_path):
        assert DirSource(tmp_path).label == str(tmp_path)


# ------------------------------------------------------------- GitHubSource

class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}        # path -> (status, bytes | dict) ; set per test
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        r = type(self).routes.get(self.path)
        if r is None:
            self.send_response(404)
            self.end_headers()
            return
        if r == "sleep":
            time.sleep(2.0)
            self.send_response(200)
            self.end_headers()
            return
        status, body = r
        if status in (301, 302):
            self.send_response(status)
            self.send_header("Location", body)
            self.end_headers()
            return
        if r == "truncate":
            return
        if status == 206:   # 声明大 Content-Length 但提前断开
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status == 200:
            self.wfile.write(body)


@pytest.fixture
def server():
    Handler.routes = {}
    Handler.hits = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    t.join()


def gh(base, **kw):
    return GitHubSource(base_url=base, proxy="", **kw)


MANIFEST_BODY = json.dumps(make_manifest(
    "memscope", "1.0.0", name="memscope-1.0.0.exe", size=3,
    sha256="a" * 64)).encode()


class TestGitHubSource:
    MURL = "/o/r/releases/latest/download/perfbox-tool.json"

    def test_manifest_follows_redirect(self, server):
        Handler.routes = {
            self.MURL: (302, "/final/perfbox-tool.json"),
            "/final/perfbox-tool.json": (200, MANIFEST_BODY),
        }
        raw = gh(server).latest_manifest(entry_of())
        assert parse(raw).id == "memscope"

    def test_manifest_404_returns_none(self, server):
        assert gh(server).latest_manifest(entry_of()) is None

    def test_manifest_500_raises_after_retry(self, server):
        Handler.routes = {self.MURL: (500, b"boom")}
        with pytest.raises(SourceError) as ei:
            gh(server).latest_manifest(entry_of())
        assert self.MURL in str(ei.value)
        assert Handler.hits.count(self.MURL) == 2  # 重试一次

    def test_artifact_download(self, server, tmp_path):
        body = b"exe-bytes-123456"
        m = parse(MANIFEST_BODY)
        aurl = "/o/r/releases/download/v1.0.0/memscope-1.0.0.exe"
        Handler.routes = {
            aurl: (302, "/dl/file.exe"),
            "/dl/file.exe": (200, body),
        }
        dest = tmp_path / "a.part"
        calls = []
        gh(server).fetch_artifact(entry_of(), m, dest,
                                  lambda d, t: calls.append((d, t)))
        assert dest.read_bytes() == body
        assert calls[-1][0] == len(body) and calls[-1][1] == len(body)

    def test_artifact_404_raises(self, server, tmp_path):
        m = parse(MANIFEST_BODY)
        with pytest.raises(SourceError):
            gh(server).fetch_artifact(entry_of(), m, tmp_path / "a.part", None)

    def test_artifact_mid_body_disconnect(self, server, tmp_path):
        aurl = "/o/r/releases/download/v1.0.0/memscope-1.0.0.exe"
        Handler.routes = {aurl: (206, b"short")}  # 声明 1000 只发 5
        m = parse(MANIFEST_BODY)
        with pytest.raises(SourceError):
            gh(server).fetch_artifact(entry_of(), m, tmp_path / "a.part", None)

    def test_timeout(self, server, tmp_path):
        Handler.routes = {self.MURL: "sleep"}
        with pytest.raises(SourceError):
            gh(server, timeout=0.3).latest_manifest(entry_of())

    def test_manifest_oversize_rejected(self, server):
        Handler.routes = {self.MURL: (200, b"x" * (1 << 20) + b"!")}
        with pytest.raises(SourceError):
            gh(server).latest_manifest(entry_of())


# ------------------------------------------------------------- configured

class TestConfigured:
    def test_env_dir_source_wins(self, tmp_path, monkeypatch):
        env = {"PERFBOX_SOURCE": str(tmp_path)}
        cfg = Config(sources=[{"type": "github"}])
        result = configured(cfg, env)
        assert len(result) == 1 and isinstance(result[0], DirSource)
        assert result[0].root == tmp_path

    def test_env_empty_falls_through(self):
        cfg = Config(sources=[{"type": "github"}])
        result = configured(cfg, {"PERFBOX_SOURCE": ""})
        assert len(result) == 1 and isinstance(result[0], GitHubSource)

    def test_sources_in_order(self):
        cfg = Config(sources=[{"type": "dir", "path": "D:\\tools"},
                              {"type": "github"},
                              {"type": "github",
                               "base_url": "http://mirror.local"}],
                     proxy="http://127.0.0.1:7890")
        result = configured(cfg, {})
        assert [type(s) for s in result] == [DirSource, GitHubSource,
                                             GitHubSource]
        assert result[0].root == Path("D:\\tools")
        assert result[1].base_url == "https://github.com"
        assert result[2].base_url == "http://mirror.local"
