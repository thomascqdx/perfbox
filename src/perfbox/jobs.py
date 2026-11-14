"""任务：子进程的生命周期。

结果目录、排队并发、Windows Job Object（按进程树结束）、日志落盘与
解码、按清单 outputs 收集结果、job.json 持久化。布局与流程见
implementation-plan 3.3/3.4。
"""

from __future__ import annotations

import json
import locale
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from perfbox import store
from perfbox.manifest import ParamError
from perfbox.store import InstalledTool

_JOB_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]*")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class JobError(Exception):
    """提交被拒（目录不可写或已存在、参数非法）。"""


class JobNotFound(JobError):
    """任务 id 不存在。"""


@dataclass
class Job:
    id: str
    tool: str
    version: str
    command: str
    values: dict
    argv: list[str]
    out_dir: str
    state: str              # queued|running|succeeded|failed|cancelled|interrupted
    exit_code: int | None
    created: float
    started: float | None
    finished: float | None
    outputs: list[dict]
    error: str | None
    host_pid: int | None        # 提交任务的宿主进程 pid（旧记录为 None）
    command_title: str | None   # 清单里的命令名（旧记录为 None）

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in _FIELDS}
        d["result_missing"] = (self.state in TERMINAL
                               and not Path(self.out_dir).exists())
        return d


_FIELDS = ("id", "tool", "version", "command", "values", "argv",
           "out_dir", "state", "exit_code", "created", "started",
           "finished", "outputs", "error", "host_pid", "command_title")


@dataclass
class App:
    """应用型命令的一个运行实例（只存在于内存，不落盘）。"""

    tool: str
    version: str
    command: str
    command_title: str | None
    state: str              # starting | ready | failed | exited
    url: str | None
    pid: int | None
    port: int
    started: float
    error: str | None
    _lines: list = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in _APP_FIELDS}
        d["log"] = "".join(list(self._lines))
        return d


_APP_FIELDS = ("tool", "version", "command", "command_title", "state",
               "url", "pid", "port", "started", "error")


class _AppRt:
    """应用的运行时状态（进程、Job Object、就绪事件、版本占用）。"""

    __slots__ = ("proc", "winjob", "command", "use_exit", "evt", "regex")

    def __init__(self, proc, winjob, command, use_exit, regex):
        self.proc = proc
        self.winjob = winjob
        self.command = command
        self.use_exit = use_exit
        self.evt = threading.Event()
        self.regex = regex


class _Rt:
    """一次会话里任务的运行时状态（不持久化）。"""

    __slots__ = ("proc", "winjob", "command", "use_exit", "cancelled")

    def __init__(self, command, use_exit):
        self.proc = None
        self.winjob = None
        self.command = command
        self.use_exit = use_exit      # 释放 store.use(tool) 的回调
        self.cancelled = False


# ------------------------------------------------- Windows Job Object
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount",
                     "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class _JobBasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD)]

    class _JobExtLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _KILL_ON_CLOSE = 0x2000        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _EXT_LIMIT_INFO = 9            # JobObjectExtendedLimitInformation

    class _WinJob:
        """KILL_ON_JOB_CLOSE 的 Job Object；terminate 按进程树结束。"""

        def __init__(self):
            k = ctypes.windll.kernel32
            self._k = k
            self.handle = k.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError("CreateJobObjectW 失败")
            info = _JobExtLimit()
            info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE
            if not k.SetInformationJobObject(
                    self.handle, _EXT_LIMIT_INFO,
                    ctypes.byref(info), ctypes.sizeof(info)):
                k.CloseHandle(self.handle)
                self.handle = None
                raise OSError("SetInformationJobObject 失败")

        def assign(self, proc):
            if not self._k.AssignProcessToJobObject(self.handle,
                                                    proc._handle):
                raise OSError("AssignProcessToJobObject 失败")

        def terminate(self):
            if not self._k.TerminateJobObject(self.handle, 1):
                raise OSError("TerminateJobObject 失败")

        def close(self):
            if self.handle:
                self._k.CloseHandle(self.handle)
                self.handle = None


def _child_env() -> dict:
    """工具子进程环境；冻结运行时让子 exe 的引导器复位继承的环境。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _decode_line(raw: bytes, loc: str) -> str:
    """UTF-8 → 本机 ANSI 代码页 → UTF-8 replace。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode(loc)
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _pid_alive(pid) -> bool:
    """进程是否还活着；None/非整数一律视为不存在。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.GetLastError() == 5   # ACCESS_DENIED → 活着
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259            # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(proc, winjob) -> None:
    """结束整棵进程树：优先 TerminateJobObject，退回 taskkill /T /F。"""
    if winjob is not None:
        try:
            winjob.terminate()
            return
        except OSError:
            pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            proc.kill()
        except OSError:
            pass


class Jobs:
    def __init__(self, home: Path, workdir: Path, max_parallel: int = 1):
        self._home = Path(home)
        self._workdir = Path(workdir)
        self._jobs: dict[str, Job] = {}
        self._rt: dict[str, _Rt] = {}
        self._apps: dict[str, App] = {}
        self._app_rt: dict[str, _AppRt] = {}
        self._app_locks: dict[str, threading.Lock] = {}
        self._cond = threading.Condition()
        self._queue: queue.Queue = queue.Queue()
        self._dead = False
        self._workers = []
        self._load()
        for _ in range(max(1, max_parallel)):
            t = threading.Thread(target=self._worker, daemon=True,
                                 name="perfbox-job-worker")
            t.start()
            self._workers.append(t)

    # ------------------------------------------------------- 持久化
    def _save(self, job: Job) -> None:
        d = self._home / "jobs" / job.id
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix="job.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({k: getattr(job, k) for k in _FIELDS}, f,
                          ensure_ascii=False)
            # Windows 上别的宿主正在读 job.json 时 replace 会被拒，稍等重试
            for attempt in range(50):
                try:
                    os.replace(tmp, d / "job.json")
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.02)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _from_dict(d) -> Job:
        if not isinstance(d, dict):
            raise ValueError("job.json 不是对象")
        kw = {k: d[k] for k in _FIELDS
              if k not in ("host_pid", "command_title")}
        kw["host_pid"] = d.get("host_pid")
        kw["command_title"] = d.get("command_title")
        return Job(**kw)

    def _mark_stale(self, job: Job) -> None:
        """宿主进程已死的 queued/running 任务标记为 interrupted。"""
        if job.state in ("queued", "running") and \
                not _pid_alive(job.host_pid):
            job.state = "interrupted"
            job.error = "perfbox 退出时任务未完成"
            job.finished = job.finished or time.time()
            self._save(job)

    def _scan_dir(self, into: dict | None = None) -> None:
        """扫 home/jobs/*/job.json；本实例的任务以内存为准，其余以磁盘为准。"""
        jd = self._home / "jobs"
        if not jd.is_dir():
            return
        loaded = []
        for sub in jd.iterdir():
            f = sub / "job.json"
            if not sub.is_dir() or sub.name in self._rt or \
                    not f.is_file():
                continue
            try:
                job = self._from_dict(
                    json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            self._mark_stale(job)
            loaded.append(job)
        with self._cond:
            for job in loaded:
                if job.id not in self._rt:
                    self._jobs[job.id] = job

    def _load(self) -> None:
        self._scan_dir()

    # ------------------------------------------------------- 提交
    def start_job(self, tool: InstalledTool, command_id: str,
                  values: dict, out_dir: Path | str | None = None) -> Job:
        if self._dead:
            raise JobError("任务系统已关闭")
        try:
            cmd = tool.manifest.command(command_id)
        except KeyError:
            raise JobError(f"未知命令：{command_id}") from None
        if cmd.kind != "job":
            raise JobError(f"{command_id} 不是任务型命令")
        if not isinstance(values, dict):
            raise JobError("values 必须是参数名到值的对象")

        job_id = (time.strftime("%Y%m%d-%H%M%S") + f"-{tool.id}-"
                  + secrets.token_hex(2))
        if out_dir is None:
            od = self._workdir / "out" / job_id
        else:
            od = Path(out_dir)
            if not od.is_absolute():
                od = self._workdir / od
        try:
            argv = tool.argv(command_id, values, od)
        except ParamError as e:
            raise JobError(str(e)) from e

        if cmd.output.fresh:
            if od.exists():
                raise JobError(f"结果目录已存在：{od}")
            probe_dir = od.parent
        else:
            probe_dir = od
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise JobError(f"结果目录不可写：{od}（{e}）") from e
        try:
            fd, tmp = tempfile.mkstemp(dir=probe_dir, prefix=".perfbox-")
            os.close(fd)
            os.unlink(tmp)
        except OSError as e:
            raise JobError(f"结果目录不可写：{probe_dir}（{e}）") from e

        job = Job(id=job_id, tool=tool.id, version=tool.version,
                  command=command_id, values=dict(values), argv=argv,
                  out_dir=str(od), state="queued", exit_code=None,
                  created=time.time(), started=None, finished=None,
                  outputs=[], error=None, host_pid=os.getpid(),
                  command_title=cmd.title)
        self._save(job)
        (self._home / "jobs" / job.id / "log.txt").touch()
        # 占用已装版本，直到任务终态，防止被剪枝
        ctx = store.use(tool)
        ctx.__enter__()
        rt = _Rt(cmd, lambda: ctx.__exit__(None, None, None))
        with self._cond:
            self._jobs[job.id] = job
            self._rt[job.id] = rt
            self._queue.put(job.id)
            self._cond.notify_all()
        return job

    # ------------------------------------------------------- 查询
    def get(self, job_id: str) -> Job:
        with self._cond:
            if job_id in self._rt:
                return self._jobs[job_id]
        # 别家宿主的任务：每次从磁盘取最新
        if not _JOB_ID_RE.fullmatch(job_id):
            raise JobNotFound(f"任务不存在：{job_id}")
        f = self._home / "jobs" / job_id / "job.json"
        try:
            job = self._from_dict(
                json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise JobNotFound(f"任务不存在：{job_id}") from e
        self._mark_stale(job)
        return job

    def history(self) -> list[Job]:
        self._scan_dir()               # 别家宿主的新任务/进度
        with self._cond:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: (j.created, j.id), reverse=True)
        return jobs

    def read_log(self, job_id: str, offset: int) -> tuple[str, int]:
        self.get(job_id)
        try:
            data = (self._home / "jobs" / job_id / "log.txt").read_bytes()
        except OSError:
            return "", 0
        offset = max(0, min(int(offset), len(data)))
        nl = data.rfind(b"\n", offset)
        if nl < 0:
            return "", offset
        return (data[offset:nl + 1].decode("utf-8", errors="replace"),
                nl + 1)

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        job = self.get(job_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while job.state not in TERMINAL:
                rem = None if deadline is None else \
                    deadline - time.monotonic()
                if rem is not None and rem <= 0:
                    break
                self._cond.wait(rem)
        return job

    # ------------------------------------------------------- 取消与关停
    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._cond:
            rt = self._rt.get(job_id)
            if job.state in TERMINAL:
                return
            if rt is None:
                raise JobError(
                    "任务由另一个 perfbox 窗口运行，请在那个窗口里取消")
            if job.state == "queued":
                self._finish_locked(job, rt, "cancelled")
                return
            rt.cancelled = True
            proc = rt.proc
        if proc is not None:
            _kill_tree(proc, rt.winjob)

    def shutdown(self) -> None:
        with self._cond:
            if self._dead:
                return
            self._dead = True
            live = [(self._jobs[i], rt) for i, rt in self._rt.items()
                    if i in self._jobs]
        for job, rt in live:
            with self._cond:
                st = job.state
                if st == "running":
                    rt.cancelled = True
                proc = rt.proc if st == "running" else None
            if st == "queued":
                self._finish(job, rt, "cancelled")
            elif proc is not None:
                _kill_tree(proc, rt.winjob)
        for job, _ in live:
            if job.state == "running":
                self.wait(job.id, 60)
        for tid, rt in list(self._app_rt.items()):
            app = self._apps.get(tid)
            with self._cond:
                if app is not None and app.state in ("starting", "ready"):
                    app.state = "exited"
                rt.evt.set()
            _kill_tree(rt.proc, rt.winjob)
        for _ in self._workers:
            self._queue.put(None)
        for t in self._workers:
            t.join(10)

    # ------------------------------------------------------- 应用
    def start_app(self, tool: InstalledTool, command_id: str,
                  timeout: float = 30.0) -> App:
        """启动应用型命令；同一工具同一时刻只保留一个实例。"""
        if self._dead:
            raise JobError("任务系统已关闭")
        try:
            cmd = tool.manifest.command(command_id)
        except KeyError:
            raise JobError(f"未知命令：{command_id}") from None
        if cmd.kind != "app":
            raise JobError(f"{command_id} 不是应用型命令")
        if not cmd.ready_url_regex:
            raise JobError(f"{command_id} 缺少 ready.url_regex")
        with self._cond:
            lock = self._app_locks.setdefault(tool.id, threading.Lock())
        with lock:
            cur = self._apps.get(tool.id)
            rt0 = self._app_rt.get(tool.id)
            if cur is not None and rt0 is not None and \
                    cur.state in ("starting", "ready") and \
                    rt0.proc.poll() is None:
                return cur                      # 已在跑，直接复用
            port = _free_port()
            try:
                argv = tool.argv(command_id, {}, None, port)
            except ParamError as e:
                raise JobError(str(e)) from e
            app = App(tool=tool.id, version=tool.version,
                      command=command_id, command_title=cmd.title,
                      state="starting", url=None, pid=None, port=port,
                      started=time.time(), error=None)
            env = _child_env()
            try:
                proc = subprocess.Popen(
                    argv, cwd=str(self._workdir),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                    creationflags=getattr(subprocess,
                                          "CREATE_NO_WINDOW", 0))
            except OSError as e:
                app.state = "failed"
                app.error = f"启动失败：{e}"
                raise JobError(app.error) from e
            app.pid = proc.pid
            winjob = None
            if os.name == "nt":
                try:
                    winjob = _WinJob()
                    winjob.assign(proc)
                except OSError:
                    winjob = None
            ctx = store.use(tool)               # 占用版本直到应用退出
            ctx.__enter__()
            rt = _AppRt(proc, winjob, cmd,
                        lambda: ctx.__exit__(None, None, None),
                        re.compile(cmd.ready_url_regex))
            with self._cond:
                self._apps[tool.id] = app
                self._app_rt[tool.id] = rt
            threading.Thread(target=self._app_monitor, args=(app, rt),
                             daemon=True,
                             name=f"perfbox-app-{tool.id}").start()
            if not rt.evt.wait(timeout):
                self._fail_app(app, rt, f"等待就绪超时（{timeout:.0f} 秒）")
            if app.state != "ready":
                self._fail_app(app, rt, "进程提前退出")
            return app

    def _fail_app(self, app: App, rt: _AppRt, reason: str):
        tail = "".join(list(app._lines)[-20:])
        with self._cond:
            app.state = "failed"
            app.error = reason
        rt.evt.set()
        _kill_tree(rt.proc, rt.winjob)
        msg = f"{app.command_title or app.command}：{reason}"
        raise JobError(msg + (f"，最近日志：\n{tail}".rstrip()
                              if tail else ""))

    def apps(self) -> list[App]:
        """存活中的应用实例（starting/ready）。"""
        return [a for a in self._apps.values()
                if a.state in ("starting", "ready")]

    def stop_app(self, tool_id: str) -> None:
        """结束一个工具的应用进程树；没有实例时无操作。"""
        rt = self._app_rt.get(tool_id)
        if rt is None:
            return
        app = self._apps.get(tool_id)
        with self._cond:
            if app is not None and app.state in ("starting", "ready"):
                app.state = "exited"
            rt.evt.set()
        _kill_tree(rt.proc, rt.winjob)

    def _app_monitor(self, app: App, rt: _AppRt) -> None:
        """读应用 stdout：就绪匹配 + 持续排空 + 退出检测。"""
        loc = locale.getencoding()
        try:
            for raw in rt.proc.stdout:
                line = _decode_line(raw, loc).replace("\r\n", "\n")
                with self._cond:
                    app._lines.append(line)
                    if len(app._lines) > 200:
                        del app._lines[:-200]
                if app.state == "starting" and rt.regex is not None:
                    m = rt.regex.search(line)
                    if m:
                        with self._cond:
                            if app.state == "starting":
                                app.url = m.group(0)
                                app.state = "ready"
                        rt.evt.set()
        except OSError:
            pass
        rt.proc.wait()
        with self._cond:
            if app.state in ("starting", "ready"):
                app.state = "exited"
            if self._app_rt.get(app.tool) is rt:
                self._apps.pop(app.tool, None)
                self._app_rt.pop(app.tool, None)
            if rt.use_exit is not None:
                rt.use_exit()
                rt.use_exit = None
            rt.evt.set()
        if rt.winjob is not None:
            rt.winjob.close()

    # ------------------------------------------------------- 内部
    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            with self._cond:
                job = self._jobs.get(job_id)
                rt = self._rt.get(job_id)
            if self._dead or job is None or rt is None or \
                    job.state != "queued":
                continue
            self._run(job, rt)

    def _set_state(self, job: Job, state: str, **kw) -> None:
        with self._cond:
            job.state = state
            for k, v in kw.items():
                setattr(job, k, v)
            self._save(job)
            self._cond.notify_all()

    def _finish_locked(self, job: Job, rt: _Rt, state: str,
                       exit_code: int | None = None) -> None:
        job.state = state
        job.exit_code = exit_code
        job.finished = time.time()
        if rt.use_exit is not None:
            rt.use_exit()
            rt.use_exit = None
        self._save(job)
        self._cond.notify_all()

    def _finish(self, job: Job, rt: _Rt, state: str,
                exit_code: int | None = None) -> None:
        with self._cond:
            self._finish_locked(job, rt, state, exit_code)

    def _run(self, job: Job, rt: _Rt) -> None:
        self._set_state(job, "running", started=time.time())
        env = _child_env()
        try:
            proc = subprocess.Popen(
                job.argv, cwd=str(self._workdir),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            job.error = f"启动失败：{e}"
            self._finish(job, rt, "failed")
            return
        rt.proc = proc
        winjob = None
        if os.name == "nt":
            try:
                winjob = _WinJob()
                winjob.assign(proc)
            except OSError:
                winjob = None      # 取消时退回 taskkill /T /F
        rt.winjob = winjob
        if rt.cancelled:
            _kill_tree(proc, winjob)
        reader = threading.Thread(target=self._read_log,
                                  args=(job, proc), daemon=True)
        reader.start()
        rc = proc.wait()
        reader.join()
        if winjob is not None:
            winjob.close()
        if rt.cancelled:
            self._finish(job, rt, "cancelled", exit_code=rc)
            return
        job.outputs = self._collect(job, rt)
        self._finish(job, rt, "succeeded" if rc == 0 else "failed",
                     exit_code=rc)

    def _read_log(self, job: Job, proc) -> None:
        """逐行读子进程输出，UTF-8 → ANSI → replace 解码后落盘。"""
        loc = locale.getencoding()
        try:
            with open(self._home / "jobs" / job.id / "log.txt",
                      "ab") as f:
                ended = True
                for raw in proc.stdout:
                    s = _decode_line(raw, loc).replace("\r\n", "\n")
                    f.write(s.encode("utf-8"))
                    f.flush()
                    ended = s.endswith("\n")
                if not ended:
                    f.write(b"\n")     # 末行没有换行符也补成完整行
        except OSError:
            pass

    @staticmethod
    def _collect(job: Job, rt: _Rt) -> list[dict]:
        od = Path(job.out_dir)
        outs, seen = [], set()
        try:
            for spec in rt.command.outputs:
                for p in od.glob(spec.glob):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(od).as_posix()
                    if rel in seen:
                        continue
                    seen.add(rel)
                    outs.append({"path": rel, "role": spec.role,
                                 "primary": spec.primary})
        except OSError:
            pass
        return outs
