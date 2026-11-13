"""jobs：结果目录、排队、Job Object、日志、取消、结果收集、历史持久化。"""

import ctypes
import json
import os
from ctypes import wintypes
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from perfbox.jobs import Jobs, JobError, JobNotFound
from perfbox.manifest import (Artifact, Command, Manifest, Output,
                              OutputSpec, Param)
from perfbox.store import InstalledTool

FAKE = str(Path(__file__).with_name("fake_tool.py"))


def _param(name):
    return Param(name=name, label=name, help=None, type="string", flag=None,
                 required=False, multiple=False, count=None, default=None,
                 advanced=False, accept=None, extensions=None, choices=None)


def make_tool(*, cmd_kind="job", fresh=False, out_kind="dir",
              filename=None, outputs=None):
    specs = outputs if outputs is not None else (
        OutputSpec("**/report.html", "report", True),
        OutputSpec("**/*.md", "report", False),
        OutputSpec("**/*.xlsx", "data", False),
        OutputSpec("**/中文报告.txt", "report", False),
    )
    cmd = Command(
        id="run", title="运行", summary=None, kind=cmd_kind, args=(),
        params=(_param("spec"),),
        output=Output(flag="-o", kind=out_kind, filename=filename,
                      fresh=fresh) if cmd_kind == "job" else None,
        outputs=specs if cmd_kind == "job" else (),
        ready_url_regex="http://x" if cmd_kind == "app" else None)
    man = Manifest(
        schema=1, id="memscope", title="t", summary="s", domain="memory",
        version="1.0.0", tag="v1.0.0", min_perfbox=None,
        artifact=Artifact(name="fake", kind="exe", size=1,
                          sha256="0" * 64, entry=(sys.executable, FAKE)),
        commands=(cmd,), raw={})
    return InstalledTool(id="memscope", version="1.0.0",
                         root=Path("no-real-root"), manifest=man)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    j = Jobs(home, workdir)
    yield home, workdir, j
    j.shutdown()


def _pid_alive(pid):
    if not hasattr(ctypes, "windll"):
        return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259        # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


# ------------------------------------------------------------ 结果目录

def test_default_out_dir_and_files(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    assert job.command_title == "运行"
    assert re.fullmatch(r"\d{8}-\d{6}-memscope-[0-9a-f]{4}", job.id)
    job = j.wait(job.id, 30)
    assert job.state == "succeeded", job.error
    assert job.exit_code == 0
    assert Path(job.out_dir) == workdir / "out" / job.id
    assert (Path(job.out_dir) / "report.html").exists()
    # perfbox 自己的文件不进结果目录
    assert not (Path(job.out_dir) / "job.json").exists()
    assert not (Path(job.out_dir) / "log.txt").exists()
    # 任务记录和日志在 home/jobs/<id>/
    jdir = home / "jobs" / job.id
    assert (jdir / "job.json").is_file() and (jdir / "log.txt").is_file()


def test_cwd_is_workdir(env):
    home, workdir, j = env
    job = j.start_job(make_tool(outputs=(OutputSpec("cwd.txt", "data"),)),
                      "run", {"spec": "cwdfile=1,nofiles=1"})
    job = j.wait(job.id, 30)
    assert job.state == "succeeded"
    assert Path((Path(job.out_dir) / "cwd.txt")
                .read_text()) == workdir


def test_explicit_and_relative_out_dir(env, tmp_path):
    home, workdir, j = env
    custom = tmp_path / "custom"
    job = j.start_job(make_tool(), "run", {}, out_dir=custom)
    job = j.wait(job.id, 30)
    assert job.state == "succeeded"
    assert Path(job.out_dir) == custom
    assert (custom / "report.html").exists()

    job2 = j.start_job(make_tool(), "run", {}, out_dir="rel-dir")
    job2 = j.wait(job2.id, 30)
    assert Path(job2.out_dir) == workdir / "rel-dir"
    assert (workdir / "rel-dir" / "report.html").exists()


def test_fresh_true_creates_parent_only(env):
    home, workdir, j = env
    tool = make_tool(fresh=True)
    job = j.start_job(tool, "run", {"spec": "needfresh=1"})
    job = j.wait(job.id, 30)
    assert job.state == "succeeded", job.error


def test_fresh_true_existing_dir_rejected(env):
    home, workdir, j = env
    tool = make_tool(fresh=True)
    pre = workdir / "out" / "pre-exist"
    pre.mkdir(parents=True)
    with pytest.raises(JobError):
        j.start_job(tool, "run", {}, out_dir=pre)


def test_unwritable_dir_rejected(env, tmp_path):
    home = tmp_path / "home"
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                     # workdir 是一个文件
    j2 = Jobs(home, blocker)
    try:
        with pytest.raises(JobError) as ei:
            j2.start_job(make_tool(), "run", {})
        assert "不可写" in str(ei.value)
        assert str(blocker) in str(ei.value)
    finally:
        j2.shutdown()


def test_non_job_command_rejected(env):
    home, workdir, j = env
    with pytest.raises(JobError):
        j.start_job(make_tool(cmd_kind="app"), "run", {})


def test_unknown_command_and_bad_values(env):
    home, workdir, j = env
    with pytest.raises(JobError):
        j.start_job(make_tool(), "nope", {})
    with pytest.raises(JobError):
        j.start_job(make_tool(), "run", {"bogus-param": "x"})


# ------------------------------------------------------------ 排队与并发

def test_queue_respects_max_parallel(env):
    home, workdir, j = env
    j1 = j.start_job(make_tool(), "run", {"spec": "sleep=0.6,nofiles=1"})
    j2 = j.start_job(make_tool(), "run", {"spec": "nofiles=1"})
    time.sleep(0.15)
    assert j.get(j1.id).state == "running"
    assert j.get(j2.id).state == "queued"
    j1 = j.wait(j1.id, 30)
    j2 = j.wait(j2.id, 30)
    assert j1.state == "succeeded" and j2.state == "succeeded"


def test_cancel_queued(env):
    home, workdir, j = env
    j1 = j.start_job(make_tool(), "run", {"spec": "sleep=0.6,nofiles=1"})
    j2 = j.start_job(make_tool(), "run", {"spec": "nofiles=1"})
    j.cancel(j2.id)
    assert j.get(j2.id).state == "cancelled"
    j1 = j.wait(j1.id, 30)
    assert j1.state == "succeeded"
    j2 = j.wait(j2.id, 30)
    assert j2.state == "cancelled"
    assert not Path(j2.out_dir).exists() or \
        not (Path(j2.out_dir) / "report.html").exists()


def test_cancel_running_kills_tree(env, tmp_path):
    home, workdir, j = env
    pidfile = tmp_path / "grandchild.pid"
    job = j.start_job(make_tool(), "run",
                      {"spec": f"grandchild={pidfile},sleep=60,nofiles=1"})
    deadline = time.time() + 30
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), "grandchild pid file never appeared"
    gpid = int(pidfile.read_text())
    assert _pid_alive(gpid)
    j.cancel(job.id)
    job = j.wait(job.id, 30)
    assert job.state == "cancelled"
    deadline = time.time() + 15
    while _pid_alive(gpid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(gpid), "grandchild survived the job-object kill"


def test_cancel_terminal_is_noop(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    job = j.wait(job.id, 30)
    j.cancel(job.id)
    assert j.get(job.id).state == "succeeded"


# ------------------------------------------------------------ 退出与结果

def test_failed_exit_code_outputs_still_listed(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {"spec": "fail=7"})
    job = j.wait(job.id, 30)
    assert job.state == "failed" and job.exit_code == 7
    paths = {o["path"]: o for o in job.outputs}
    assert "report.html" in paths and paths["report.html"]["primary"]
    assert "data/data.xlsx" in paths
    assert "中文报告.txt" in paths
    roles = {o["path"]: o["role"] for o in job.outputs}
    assert roles["data/data.xlsx"] == "data"


def test_top_level_double_star(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    job = j.wait(job.id, 30)
    # "**/report.html" 必须同时命中顶层 report.html
    assert "report.html" in {o["path"] for o in job.outputs}


def test_output_kind_file(env):
    home, workdir, j = env
    tool = make_tool(out_kind="file", filename="result.txt",
                     outputs=(OutputSpec("result.txt", "report", True),))
    job = j.start_job(tool, "run", {})
    job = j.wait(job.id, 30)
    assert job.state == "succeeded", job.error
    assert (Path(job.out_dir) / "result.txt").exists()
    assert job.outputs[0]["path"] == "result.txt"


# ------------------------------------------------------------ 日志

def test_log_utf8_gbk_invalid(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run",
                      {"spec": "utf8=1,gbk=1,bad=1,nofiles=1"})
    job = j.wait(job.id, 30)
    text, off = j.read_log(job.id, 0)
    assert text.count("中文日志行") == 2          # UTF-8 行 + GBK 行
    assert "�" in text                          # 非法字节被替换
    assert off == len((home / "jobs" / job.id / "log.txt").read_bytes())


def test_read_log_incremental_and_clamp(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run",
                      {"spec": "lines=5,delay=0.15,nofiles=1"})
    first, off1 = j.read_log(job.id, 0)
    job = j.wait(job.id, 30)
    second, off2 = j.read_log(job.id, off1)
    assert "log line 0" in first + second
    assert (first + second).count("log line") == 5
    assert off2 > off1 or off1 == 0 and "log line 4" in second
    # 偏移钳制
    text, off3 = j.read_log(job.id, 10 ** 9)
    assert text == "" and off3 == off2
    text, off4 = j.read_log(job.id, -5)
    assert "log line 0" in text and off4 == off2


def test_read_log_only_complete_lines(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run",
                      {"spec": "lines=1,nofiles=1"})
    job = j.wait(job.id, 30)
    logf = home / "jobs" / job.id / "log.txt"
    with open(logf, "ab") as f:
        f.write("半行".encode("utf-8"))
    text, off = j.read_log(job.id, 0)
    assert text.endswith("\n") and "半行" not in text
    text2, off2 = j.read_log(job.id, off)
    assert text2 == "" and off2 == off


def test_log_final_line_without_newline(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run",
                      {"spec": "tail=1,nofiles=1"})
    job = j.wait(job.id, 30)
    assert job.state == "succeeded"
    text, off = j.read_log(job.id, 0)
    assert "tail-without-newline" in text
    assert text.endswith("\n")


def test_unknown_job(env):
    home, workdir, j = env
    with pytest.raises(JobNotFound):
        j.get("nope")
    with pytest.raises(JobNotFound):
        j.read_log("nope", 0)
    with pytest.raises(JobNotFound):
        j.cancel("nope")


# ------------------------------------------------------------ 持久化

def test_history_survives_restart(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    job = j.wait(job.id, 30)
    j.shutdown()
    j2 = Jobs(home, workdir)
    try:
        got = j2.get(job.id)
        assert got.state == "succeeded" and got.out_dir == job.out_dir
        hist = j2.history()
        assert hist[0].id == job.id
    finally:
        j2.shutdown()


def test_stale_running_marked_interrupted(env, tmp_path):
    home, workdir, j = env
    j.shutdown()
    jd = home / "jobs" / "20200101-000000-memscope-dead"
    jd.mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps({
        "id": "20200101-000000-memscope-dead", "tool": "memscope",
        "version": "1.0.0", "command": "run", "values": {},
        "argv": ["x"], "out_dir": str(workdir / "out" / "x"),
        "state": "running", "exit_code": None, "created": 1.0,
        "started": 1.0, "finished": None, "outputs": [], "error": None}),
        encoding="utf-8")
    j2 = Jobs(home, workdir)
    try:
        got = j2.get("20200101-000000-memscope-dead")
        assert got.state == "interrupted"
        assert "退出" in got.error
        saved = json.loads((jd / "job.json").read_text(encoding="utf-8"))
        assert saved["state"] == "interrupted"
    finally:
        j2.shutdown()


def test_corrupt_jobjson_skipped(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    job = j.wait(job.id, 30)
    bad = home / "jobs" / "corrupt"
    bad.mkdir(parents=True)
    (bad / "job.json").write_bytes(b"{not json")
    hist = j.history()
    assert all(x.id != "corrupt" for x in hist)
    assert any(x.id == job.id for x in hist)


def test_result_missing_flag(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run", {})
    job = j.wait(job.id, 30)
    assert job.to_dict()["result_missing"] is False
    shutil.rmtree(job.out_dir)
    assert j.get(job.id).to_dict()["result_missing"] is True


def test_shutdown_kills_running(env):
    home, workdir, j = env
    job = j.start_job(make_tool(), "run",
                      {"spec": "sleep=60,nofiles=1"})
    deadline = time.time() + 30
    while j.get(job.id).state != "running" and time.time() < deadline:
        time.sleep(0.05)
    assert j.get(job.id).state == "running"
    j.shutdown()
    assert j.get(job.id).state == "cancelled"


# ------------------------------------------------------------ 多宿主

def _write_jobjson(home, jid, state, **kw):
    d = home / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": jid, "tool": "memscope", "version": "1.0.0",
           "command": "run", "values": {}, "argv": ["x"],
           "out_dir": str(home / "out" / jid), "state": state,
           "exit_code": None, "created": 1.0, "started": 1.0,
           "finished": None, "outputs": [], "error": None}
    rec.update(kw)
    (d / "job.json").write_text(json.dumps(rec), encoding="utf-8")


def test_stale_marking_uses_host_pid(env):
    home, workdir, j = env
    j.shutdown()
    _write_jobjson(home, "j-alive", "running", host_pid=os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = p.pid
    p.wait()
    _write_jobjson(home, "j-dead", "running", host_pid=dead_pid)
    _write_jobjson(home, "j-nopid", "running")
    j2 = Jobs(home, workdir)
    try:
        assert j2.get("j-alive").state == "running"
        assert j2.get("j-dead").state == "interrupted"
        assert j2.get("j-nopid").state == "interrupted"
    finally:
        j2.shutdown()


def test_history_and_get_see_other_host(env):
    home, workdir, jB = env            # jB 先构造，不认识之后提交的任务
    jA = Jobs(home, workdir)
    try:
        job = jA.start_job(make_tool(), "run",
                           {"spec": "lines=1,nofiles=1"})
        assert job.id in {x.id for x in jB.history()}
        assert jB.get(job.id).tool == "memscope"
        jA.wait(job.id, 30)
        assert jB.get(job.id).state == "succeeded"
        assert {x.id: x for x in jB.history()}[job.id].state == \
            "succeeded"
    finally:
        jA.shutdown()


def test_cancel_foreign_job_rejected(env):
    home, workdir, jB = env
    jA = Jobs(home, workdir)
    try:
        job = jA.start_job(make_tool(), "run",
                           {"spec": "sleep=30,nofiles=1"})
        deadline = time.time() + 30
        while jA.get(job.id).state != "running" and \
                time.time() < deadline:
            time.sleep(0.05)
        assert jA.get(job.id).state == "running"
        with pytest.raises(JobError) as ei:
            jB.cancel(job.id)
        assert "另一个 perfbox" in str(ei.value)
        jA.cancel(job.id)
        assert jA.wait(job.id, 30).state == "cancelled"
    finally:
        jA.shutdown()


# ------------------------------------------------------------ 应用

def make_app_tool(mode="gbk"):
    cmd = Command(
        id="ui", title="界面", summary=None, kind="app",
        args=("app", "--port", "{port}", "--mode", mode),
        params=(), output=None, outputs=(),
        ready_url_regex=r"http://127\.0\.0\.1:\d+/#t=\S+")
    man = Manifest(
        schema=1, id="memscope", title="t", summary="s", domain="memory",
        version="1.0.0", tag="v1.0.0", min_perfbox=None,
        artifact=Artifact(name="fake", kind="exe", size=1,
                          sha256="0" * 64, entry=(sys.executable, FAKE)),
        commands=(cmd,), raw={})
    return InstalledTool(id="memscope", version="1.0.0",
                         root=Path("no-real-root"), manifest=man)


def test_app_start_ready_and_url(env):
    home, workdir, j = env
    app = j.start_app(make_app_tool(), "ui")
    assert app.state == "ready"
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/#t=abc", app.url)
    assert app.port > 0 and app.pid and app.started
    assert app.command_title == "界面"
    assert "已启动" in "".join(app._lines)      # GBK 行解码成功
    assert "已启动" in app.to_dict()["log"]
    assert j.apps() == [app]
    j.stop_app("memscope")
    deadline = time.time() + 10
    while j.apps() and time.time() < deadline:
        time.sleep(0.05)
    assert j.apps() == []
    assert app.state == "exited"
    assert not _pid_alive(app.pid)


def test_app_second_start_returns_same(env):
    home, workdir, j = env
    a1 = j.start_app(make_app_tool(), "ui")
    a2 = j.start_app(make_app_tool(), "ui")
    assert a1 is a2 and a2.pid == a1.pid


def test_app_pipe_is_drained(env):
    home, workdir, j = env
    app = j.start_app(make_app_tool("flood"), "ui")
    assert app.state == "ready"
    last = app._lines[-1] if app._lines else ""
    time.sleep(1.5)          # ~150 KiB 产出，远超 64 KiB 管道缓冲
    rt = j._app_rt.get("memscope")
    assert rt is not None and rt.proc.poll() is None
    assert app._lines and app._lines[-1] != last   # 仍在产出


def test_app_timeout(env):
    home, workdir, j = env
    with pytest.raises(JobError) as ei:
        j.start_app(make_app_tool("silent"), "ui", timeout=1)
    assert "超时" in str(ei.value)
    assert j.apps() == []


def test_app_exits_before_ready(env):
    home, workdir, j = env
    with pytest.raises(JobError) as ei:
        j.start_app(make_app_tool("die"), "ui", timeout=10)
    assert "端口被占用" in str(ei.value)      # 最近日志包含在错误里
    assert j.apps() == []


def test_app_exit_detected(env):
    home, workdir, j = env
    app = j.start_app(make_app_tool("shortlive"), "ui")
    assert app.state == "ready"
    deadline = time.time() + 10
    while j.apps() and time.time() < deadline:
        time.sleep(0.05)
    assert j.apps() == []
    assert app.state == "exited"


def test_app_stop_noop_and_kind_guard(env):
    home, workdir, j = env
    j.stop_app("nonexistent")                 # 不存在的工具，无操作
    with pytest.raises(JobError):
        j.start_app(make_tool(), "run")       # job 型命令不能 start_app


def test_shutdown_kills_apps(env):
    home, workdir, j = env
    app = j.start_app(make_app_tool(), "ui")
    pid = app.pid
    j.shutdown()
    deadline = time.time() + 10
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(pid)
