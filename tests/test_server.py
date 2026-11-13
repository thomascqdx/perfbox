"""server：token/Host 校验、静态文件、/api/*、/files/ 全流程。"""

import http.client
import json
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from perfbox import store
from perfbox.jobs import Jobs
from perfbox.server import Host, create_server
from perfbox.sources import DirSource

from helpers import entry_of, publish
from test_jobs import make_app_tool, make_tool


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    monkeypatch.delenv("PERFBOX_SOURCE", raising=False)
    src = tmp_path / "dirsrc"
    src.mkdir()
    j = Jobs(home, workdir)
    host = Host(workdir, jobs=j, check_updates=False,
                sources_fn=lambda: [DirSource(src)])
    srv, host = create_server(0, host)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield host, src, j
    srv.shutdown()
    srv.server_close()
    j.shutdown()


def _req(host, method, path, body=None, token=True, host_hdr=None):
    c = http.client.HTTPConnection("127.0.0.1", host.port, timeout=30)
    headers = {"Host": host_hdr or f"127.0.0.1:{host.port}"}
    if token:
        headers["X-Perfbox-Token"] = host.token if token is True else token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    ctype = r.getheader("Content-Type") or ""
    hdrs = dict(r.getheaders())
    return r.status, (json.loads(raw) if "json" in ctype else raw), hdrs


def _post_job(env, spec=""):
    host = env[0]
    body = {"tool": "memscope", "command": "run", "values": {}}
    if spec:
        body["values"] = {"spec": spec}
    st, j, _ = _req(host, "POST", "/api/jobs", body)
    assert st == 200, j
    return j


def _wait_done(host, jid, timeout=60):
    off = 0
    logs = ""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, j, _ = _req(host, "GET",
                        f"/api/jobs/{jid}?log_offset={off}")
        assert st == 200, j
        logs += j["log"]
        off = j["log_offset"]
        if j["state"] != "running" and j["state"] != "queued":
            return j, logs
        time.sleep(0.05)
    raise AssertionError("job timeout")


# ------------------------------------------------------- 认证

def test_api_requires_token(env):
    host = env[0]
    assert _req(host, "GET", "/api/state", token=False)[0] == 403
    assert _req(host, "GET", "/api/state", token="nope")[0] == 403
    assert _req(host, "GET", "/api/state")[0] == 200
    st, j, _ = _req(host, "GET", "/api/state", token=False)
    assert "error" in j


def test_api_rejects_foreign_host(env):
    host = env[0]
    assert _req(host, "GET", "/api/state",
                host_hdr="evil.example:80")[0] == 403


def test_files_requires_token_and_host(env):
    host = env[0]
    assert _req(host, "GET", "/files/nope/x/y", token=False)[0] == 403
    assert _req(host, "GET", f"/files/{host.token}/x/y",
                host_hdr="evil.example:80")[0] == 403


# ------------------------------------------------------- 静态文件

def test_static_and_traversal(env):
    host = env[0]
    st, body, _ = _req(host, "GET", "/", token=False)
    assert st == 200 and b"perfbox" in body.lower()
    assert _req(host, "GET", "/../server.py", token=False)[0] == 404
    assert _req(host, "GET", "/%2e%2e/server.py", token=False)[0] == 404


# ------------------------------------------------------- /api/state

def test_state_shape_offline(env):
    host, src, _ = env
    st, j, _ = _req(host, "GET", "/api/state")
    assert st == 200
    assert j["version"]
    assert Path(j["workdir"]) == host.workdir
    assert j["sources"] == [str(src)]
    assert {d["id"] for d in j["domains"]} == \
        {"startup", "jank", "io", "memory", "trace"}
    tools = {t["id"]: t for t in j["tools"]}
    assert "memscope" in tools and "perfetto-trim" in tools
    assert tools["memscope"]["installed"] is None
    assert tools["memscope"]["title"]


def test_state_config_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    workdir = tmp_path / "work"
    workdir.mkdir()
    j = Jobs(home, workdir)
    host = Host(workdir, jobs=j, check_updates=False,
                sources_fn=lambda: [])
    srv, host = create_server(0, host)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, body, _ = _req(host, "GET", "/api/state")
        assert st == 200 and "config_error" in body
    finally:
        srv.shutdown()
        srv.server_close()
        j.shutdown()


# ------------------------------------------------------- /api/tools/<id>

def test_tool_manifest_from_source(env):
    host, src, _ = env
    publish(src, "memscope", "9.9.9", body=b"E")
    st, j, _ = _req(host, "GET", "/api/tools/memscope")
    assert st == 200
    assert j["installed"] is None
    assert j["manifest"]["version"] == "9.9.9"


def test_tool_manifest_installed(env, monkeypatch):
    host = env[0]
    inst = make_tool()
    monkeypatch.setattr(store, "installed", lambda tid: inst)
    st, j, _ = _req(host, "GET", "/api/tools/memscope")
    assert st == 200 and j["installed"] == "1.0.0"


def test_tool_unknown_and_absent(env):
    host = env[0]
    assert _req(host, "GET", "/api/tools/nope")[0] == 404
    assert _req(host, "GET", "/api/tools/memscope")[0] == 404


# ------------------------------------------------------- /api/jobs 全流程

def test_full_job_flow(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    job = _post_job(env, "lines=3,utf8=1")
    assert job["state"] in ("queued", "running")
    assert job["out_dir"].endswith(job["id"])
    j, logs = _wait_done(host, job["id"])
    assert j["state"] == "succeeded"
    assert j["exit_code"] == 0
    assert "log line 0" in logs and "中文日志行" in logs
    outs = {o["path"]: o for o in j["outputs"]}
    assert outs["report.html"]["primary"] is True

    # /files/ 读结果
    st, body, hdrs = _req(host, "GET",
                          f"/files/{host.token}/{job['id']}/report.html")
    assert st == 200 and b"\xe6\x8a\xa5\xe5\x91\x8a" in body  # 报告
    assert "text/html" in hdrs["Content-Type"]
    st, body, hdrs = _req(host, "GET",
                          f"/files/{host.token}/{job['id']}/data/data.xlsx")
    assert st == 200
    assert "attachment" in (hdrs.get("Content-Disposition") or "")
    # 中文文件名（URL 编码）
    rel = urllib.parse.quote("中文报告.txt")
    st, body, _ = _req(host, "GET",
                       f"/files/{host.token}/{job['id']}/{rel}")
    assert st == 200 and "中文文件".encode() in body


def test_post_job_installs_when_missing(env):
    import os
    host, src, _ = env
    publish(src, "memscope", "1.0.0", body=b"not-an-exe")
    job = _post_job(env)
    # 先安装：产物落地，任务随后因无法启动失败
    assert (Path(os.environ["PERFBOX_HOME"]) / "tools" / "memscope"
            / "1.0.0" / "memscope-1.0.0.exe").is_file()
    j, _ = _wait_done(host, job["id"])
    assert j["state"] == "failed" and j["error"]


def test_post_job_validation_and_errors(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    st, j, _ = _req(host, "POST", "/api/jobs",
                    {"tool": "memscope", "command": "nope", "values": {}})
    assert st == 400 and "error" in j
    st, j, _ = _req(host, "POST", "/api/jobs",
                    {"tool": "nope", "command": "run", "values": {}})
    assert st == 400
    st, j, _ = _req(host, "GET", "/api/jobs/nope")
    assert st == 404
    st, j, _ = _req(host, "POST", "/api/jobs/nope/cancel", {})
    assert st == 404


def test_jobs_list_and_cancel(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    job = _post_job(env, "sleep=30,nofiles=1")
    st, j, _ = _req(host, "POST", f"/api/jobs/{job['id']}/cancel", {})
    assert st == 200
    j, _ = _wait_done(host, job["id"])
    assert j["state"] == "cancelled"
    st, j, _ = _req(host, "GET", "/api/jobs")
    assert st == 200 and any(x["id"] == job["id"] for x in j["jobs"])


def test_post_job_unwritable_workdir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    j = Jobs(home, blocker)
    host = Host(blocker, jobs=j, check_updates=False,
                sources_fn=lambda: [])
    srv, host = create_server(0, host)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(store, "installed", lambda tid: make_tool())
        st, body, _ = _req(host, "POST", "/api/jobs",
                           {"tool": "memscope", "command": "run",
                            "values": {}})
        assert st == 400 and "不可写" in body["error"]
    finally:
        srv.shutdown()
        srv.server_close()
        j.shutdown()


# ------------------------------------------------------- /files/ 防护

def test_files_traversal_blocked(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    job = _post_job(env)
    j, _ = _wait_done(host, job["id"])
    assert j["state"] == "succeeded"
    jid = job["id"]
    outside = Path(host.workdir) / "secret.txt"
    outside.write_text("secret")
    for rel in ("../secret.txt", "..%2Fsecret.txt",
                "%2e%2e/secret.txt", "..\\secret.txt",
                urllib.parse.quote(str(outside).replace("\\", "/"))):
        st, _, _ = _req(host, "GET", f"/files/{host.token}/{jid}/{rel}")
        assert st in (403, 404), rel
    # 结果目录本身不是文件
    st, _, _ = _req(host, "GET", f"/files/{host.token}/{jid}/data")
    assert st in (403, 404)
    # 不存在的文件
    st, _, _ = _req(host, "GET", f"/files/{host.token}/{jid}/missing.txt")
    assert st == 404
    # 别的任务 id
    st, _, _ = _req(host, "GET", f"/files/{host.token}/otherjob/report.html")
    assert st in (403, 404)


def test_foreign_disk_job_visible(env):
    import os
    host = env[0]
    # 另一个 Jobs 实例（同 home）提交任务 —— host 的实例事先不知道它
    jB = Jobs(Path(os.environ["PERFBOX_HOME"]), host.workdir)
    try:
        job = jB.start_job(make_tool(), "run", {})
        jB.wait(job.id, 30)
        assert job.state == "succeeded"
        st, j, _ = _req(host, "GET", f"/api/jobs/{job.id}")
        assert st == 200 and j["state"] == "succeeded"
        st, body, _ = _req(host, "GET",
                           f"/files/{host.token}/{job.id}/report.html")
        assert st == 200 and b"\xe6\x8a\xa5\xe5\x91\x8a" in body
    finally:
        jB.shutdown()


# ------------------------------------------------------- pick / reveal

def test_pick(env):
    host = env[0]
    host.pick = lambda kind, multiple, extensions: [r"D:\a\b.zip"]
    st, j, _ = _req(host, "POST", "/api/pick", {"kind": "file"})
    assert st == 200 and j["paths"] == [r"D:\a\b.zip"]
    host.pick = lambda kind, multiple, extensions: []
    st, j, _ = _req(host, "POST", "/api/pick", {"kind": "dir"})
    assert st == 200 and j["paths"] == []
    st, j, _ = _req(host, "POST", "/api/pick", {"kind": "bogus"})
    assert st == 400


def test_reveal(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    seen = []
    host.reveal = lambda p: seen.append(p)
    job = _post_job(env)
    j, _ = _wait_done(host, job["id"])
    st, body, _ = _req(host, "POST", "/api/reveal",
                       {"job": job["id"], "path": "report.html"})
    assert st == 200
    assert seen and seen[0].endswith("report.html")
    st, body, _ = _req(host, "POST", "/api/reveal",
                       {"job": job["id"], "path": "../x"})
    assert st == 400
    st, body, _ = _req(host, "POST", "/api/reveal",
                       {"job": job["id"], "path": "missing.txt"})
    assert st == 404
    st, body, _ = _req(host, "POST", "/api/reveal",
                       {"job": "nope", "path": "x"})
    assert st == 404


# ------------------------------------------------------- M3：状态字段

def test_state_m3_fields(env):
    host = env[0]
    st, j, _ = _req(host, "GET", "/api/state")
    assert st == 200
    assert "checked" in j and "checking" in j
    t = {t["id"]: t for t in j["tools"]}["memscope"]
    for k in ("previous", "latest", "update", "incompatible", "op", "app"):
        assert k in t
    assert t["update"] is None and t["op"] is None and t["app"] is None


def test_updates_check_endpoint(env):
    host, src, _ = env
    publish(src, "memscope", "3.3.3", body=b"E")
    st, j, _ = _req(host, "POST", "/api/updates/check", {})
    assert st == 200
    deadline = time.time() + 15
    while time.time() < deadline:
        st, j, _ = _req(host, "GET", "/api/state")
        if not j["checking"] and j["checked"] > 0:
            break
        time.sleep(0.1)
    t = {t["id"]: t for t in j["tools"]}["memscope"]
    assert t["latest"] == "3.3.3"
    assert t["update"] is None          # 未安装 → 不算"可更新"


def test_state_update_flag(env):
    host, src, _ = env
    publish(src, "memscope", "1.0.0", body=b"v1")
    store.install(entry_of(), [DirSource(src)])
    publish(src, "memscope", "2.0.0", body=b"v2")
    host.recheck_updates(force=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        st, j, _ = _req(host, "GET", "/api/state")
        if not j["checking"] and j["checked"] > 0:
            break
        time.sleep(0.1)
    t = {t["id"]: t for t in j["tools"]}["memscope"]
    assert t["installed"] == "1.0.0"
    assert t["latest"] == "2.0.0" and t["update"] == "2.0.0"


# ------------------------------------------------------- M3：安装操作

def _wait_op(host, tid, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, d, _ = _req(host, "GET", "/api/ops")
        assert st == 200
        op = d["ops"].get(tid)
        if op is not None and op["finished"]:
            return op
        time.sleep(0.05)
    raise AssertionError("op timeout")


def test_install_op_flow(env):
    host, src, _ = env
    publish(src, "memscope", "2.0.0", body=b"EXE")
    st, op, _ = _req(host, "POST", "/api/tools/memscope/install", {})
    assert st == 202
    assert op["kind"] == "install" and op["finished"] is False
    op = _wait_op(host, "memscope")
    assert op["error"] is None
    inst = store.installed("memscope")
    assert inst is not None and inst.version == "2.0.0"
    st, j, _ = _req(host, "GET", "/api/state")
    t = {t["id"]: t for t in j["tools"]}["memscope"]
    assert t["installed"] == "2.0.0"
    assert t["latest"] == "2.0.0"       # 装完缓存条目已刷新


def test_install_op_progress(env, monkeypatch):
    host = env[0]

    def fake_install(entry, srcs, progress=None):
        if progress:
            progress(5, 10)
        return make_tool()

    monkeypatch.setattr(store, "install", fake_install)
    st, op, _ = _req(host, "POST", "/api/tools/memscope/update", {})
    assert st == 202 and op["kind"] == "update"
    op = _wait_op(host, "memscope")
    assert op["done"] == 5 and op["total"] == 10


def test_install_op_double_409(env, monkeypatch):
    host = env[0]
    ev = threading.Event()

    def slow(entry, srcs, progress=None):
        ev.wait(10)
        return make_tool()

    monkeypatch.setattr(store, "install", slow)
    st, op, _ = _req(host, "POST", "/api/tools/memscope/install", {})
    assert st == 202
    st, j, _ = _req(host, "POST", "/api/tools/memscope/install", {})
    assert st == 409
    st, j, _ = _req(host, "POST", "/api/tools/memscope/update", {})
    assert st == 409
    ev.set()
    _wait_op(host, "memscope")


def test_install_op_unknown_tool(env):
    host = env[0]
    st, j, _ = _req(host, "POST", "/api/tools/nope/install", {})
    assert st == 404


def test_install_op_incompatible(env):
    host, src, _ = env
    publish(src, "memscope", "9.9.9",
            manifest_extra={"schema": 2})
    st, op, _ = _req(host, "POST", "/api/tools/memscope/install", {})
    assert st == 202
    op = _wait_op(host, "memscope")
    assert op["error"] and "perfbox" in op["error"]
    assert store.installed("memscope") is None   # 旧版本不受影响


# ------------------------------------------------------- M3：回退/卸载

def test_rollback(env):
    home_src = env[1]
    host = env[0]
    publish(home_src, "memscope", "1.0.0", body=b"v1")
    store.install(entry_of(), [DirSource(home_src)])
    publish(home_src, "memscope", "1.1.0", body=b"v2")
    store.install(entry_of(), [DirSource(home_src)])
    st, j, _ = _req(host, "POST", "/api/tools/memscope/rollback", {})
    assert st == 200 and j["installed"] == "1.0.0"
    assert store.installed("memscope").version == "1.0.0"


def test_rollback_no_previous_409(env):
    host = env[0]
    st, j, _ = _req(host, "POST", "/api/tools/memscope/rollback", {})
    assert st == 409


def test_remove_tool(env):
    host, src, _ = env
    publish(src, "memscope", "1.0.0", body=b"v1")
    store.install(entry_of(), [DirSource(src)])
    st, j, _ = _req(host, "POST", "/api/tools/memscope/remove", {})
    assert st == 200 and j["removed"] == "memscope"
    assert store.installed("memscope") is None


def test_remove_refused_while_job_running(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    job = _post_job(env, "sleep=30,nofiles=1")
    deadline = time.time() + 10
    while time.time() < deadline:
        st, j, _ = _req(host, "GET", f"/api/jobs/{job['id']}")
        if j["state"] == "running":
            break
        time.sleep(0.05)
    st, j, _ = _req(host, "POST", "/api/tools/memscope/remove", {})
    assert st == 409 and "先停止" in j["error"]
    _req(host, "POST", f"/api/jobs/{job['id']}/cancel", {})


def test_remove_refused_while_app_running(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed",
                        lambda tid: make_app_tool())
    st, app, _ = _req(host, "POST", "/api/apps",
                      {"tool": "memscope", "command": "ui"})
    assert st == 200
    st, j, _ = _req(host, "POST", "/api/tools/memscope/remove", {})
    assert st == 409 and "先停止" in j["error"]
    _req(host, "POST", "/api/apps/memscope/stop", {})


# ------------------------------------------------------- M3：应用

def test_apps_flow(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_app_tool())
    st, app, _ = _req(host, "POST", "/api/apps",
                      {"tool": "memscope", "command": "ui"})
    assert st == 200
    assert app["state"] == "ready" and app["url"].startswith("http://127.")
    assert "已启动" in app["log"]
    st, d, _ = _req(host, "GET", "/api/apps")
    assert st == 200 and [a["tool"] for a in d["apps"]] == ["memscope"]
    # 同工具再起返回同一实例
    st, app2, _ = _req(host, "POST", "/api/apps",
                       {"tool": "memscope", "command": "ui"})
    assert st == 200 and app2["pid"] == app["pid"]
    # state 里带 app
    st, j, _ = _req(host, "GET", "/api/state")
    t = {t["id"]: t for t in j["tools"]}["memscope"]
    assert t["app"]["state"] == "ready"
    st, j, _ = _req(host, "POST", "/api/apps/memscope/stop", {})
    assert st == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        st, d, _ = _req(host, "GET", "/api/apps")
        if not d["apps"]:
            return
        time.sleep(0.05)
    raise AssertionError("app 未退出")


def test_app_bad_command_400(env, monkeypatch):
    host = env[0]
    monkeypatch.setattr(store, "installed", lambda tid: make_tool())
    st, j, _ = _req(host, "POST", "/api/apps",
                    {"tool": "memscope", "command": "run"})
    assert st == 400
    st, j, _ = _req(host, "POST", "/api/apps",
                    {"tool": "nope", "command": "ui"})
    assert st == 400


def test_app_install_then_fail_400(env):
    host, src, _ = env
    publish(src, "memscope", "1.0.0", body=b"not-an-exe",
            manifest_extra={"commands": [{
                "id": "ui", "title": "界面", "kind": "app",
                "args": ["ui"], "ready": {"url_regex": "http://x"}}]})
    st, j, _ = _req(host, "POST", "/api/apps",
                    {"tool": "memscope", "command": "ui"})
    assert st == 400 and "error" in j


# ------------------------------------------------------- M3：配置

def test_config_get_default(env):
    host = env[0]
    st, cfg, _ = _req(host, "GET", "/api/config")
    assert st == 200
    assert cfg["path"].endswith("config.json")
    assert cfg["sources"] == [{"type": "github"}]
    assert cfg["proxy"] is None
    assert cfg["max_parallel_jobs"] == 1
    assert cfg["error"] is None


def test_config_roundtrip(env):
    host, src, _ = env
    st, cfg, _ = _req(host, "POST", "/api/config", {
        "sources": [{"type": "dir", "path": str(src)}],
        "proxy": "http://127.0.0.1:7890",
        "max_parallel_jobs": 3})
    assert st == 200
    assert cfg["max_parallel_jobs"] == 3
    assert cfg["restart_needed"] is True
    st, cfg, _ = _req(host, "GET", "/api/config")
    assert cfg["sources"] == [{"type": "dir", "path": str(src)}]
    assert cfg["proxy"] == "http://127.0.0.1:7890"
    assert cfg["max_parallel_jobs"] == 3
    # 并发数已保存为 3，但运行中仍是 1 → 仍提示需要重启
    st, cfg, _ = _req(host, "POST", "/api/config",
                      {"sources": [{"type": "github"}]})
    assert st == 200 and cfg["restart_needed"] is True
    # 改回与运行中一致的值 → 无需重启
    st, cfg, _ = _req(host, "POST", "/api/config",
                      {"max_parallel_jobs": 1})
    assert st == 200 and "restart_needed" not in cfg


def test_config_invalid_400(env):
    host = env[0]
    st, j, _ = _req(host, "POST", "/api/config",
                    {"max_parallel_jobs": 0})
    assert st == 400 and "error" in j
    st, j, _ = _req(host, "POST", "/api/config",
                    {"sources": [{"type": "bogus"}]})
    assert st == 400
    st, j, _ = _req(host, "POST", "/api/config",
                    {"sources": "notalist"})
    assert st == 400


def test_config_invalid_file_returns_defaults(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    workdir = tmp_path / "work"
    workdir.mkdir()
    j = Jobs(home, workdir)
    host = Host(workdir, jobs=j, check_updates=False,
                sources_fn=lambda: [])
    srv, host = create_server(0, host)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, cfg, _ = _req(host, "GET", "/api/config")
        assert st == 200
        assert cfg["error"]
        assert cfg["max_parallel_jobs"] == 1
        assert cfg["sources"] == [{"type": "github"}]
    finally:
        srv.shutdown()
        srv.server_close()
        j.shutdown()


def test_tool_manifest_incompatible_409(env):
    host, src, _ = env
    publish(src, "memscope", "9.9.9",
            manifest_extra={"schema": 2})
    st, j, _ = _req(host, "GET", "/api/tools/memscope")
    assert st == 409 and "perfbox" in j["error"]
