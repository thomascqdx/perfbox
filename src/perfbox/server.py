"""本机界面服务（只用标准库）。

只监听 127.0.0.1（默认随机端口）。每个 /api 请求必须带每次启动随机的
X-Perfbox-Token 头，且 Host 头为 127.0.0.1/localhost（防 DNS 重绑定）；
token 经 URL fragment 交给页面，永远不会发回服务器。/files/<token>/…
把 token 放在路径里，让报告里的相对链接仍然有效。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import perfbox
from perfbox import catalog, paths, sources, store
from perfbox.catalog import UnknownTool
from perfbox.jobs import Jobs, JobError, JobNotFound
from perfbox.manifest import ManifestError, ParamError
from perfbox.paths import ConfigError
from perfbox.sources import SourceError
from perfbox.store import StoreError

STATIC = Path(__file__).with_name("static")
MAX_BODY = 1 << 20

_STATIC_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png"}

_FILE_TYPES = {".html": "text/html; charset=utf-8",
               ".md": "text/plain; charset=utf-8",
               ".txt": "text/plain; charset=utf-8",
               ".csv": "text/plain; charset=utf-8",
               ".log": "text/plain; charset=utf-8",
               ".json": "application/json",
               ".png": "image/png",
               ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg",
               ".svg": "image/svg+xml",
               ".css": "text/css",
               ".js": "text/javascript",
               ".xlsx": "application/vnd.openxmlformats-"
                        "officedocument.spreadsheetml.sheet"}

_DOMAINS = [{"id": "startup", "title": "启动"},
            {"id": "jank", "title": "卡顿"},
            {"id": "io", "title": "IO"},
            {"id": "memory", "title": "内存"},
            {"id": "trace", "title": "Trace"}]

_SUBMIT_ERRORS = (JobError, ParamError, StoreError, SourceError,
                  ManifestError, UnknownTool)


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _tk_pick(kind: str, multiple: bool, extensions) -> list:
    """原生文件/目录选择框（tkinter，在独立线程里跑）。"""
    out = {}

    def dlg():
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            ftypes = []
            if extensions:
                ftypes.append(("指定类型",
                               " ".join(f"*{e}" for e in extensions)))
            ftypes.append(("所有文件", "*.*"))
            if kind == "dir":
                r = filedialog.askdirectory(parent=root)
                out["paths"] = [r] if r else []
            elif multiple:
                out["paths"] = list(filedialog.askopenfilenames(
                    parent=root, filetypes=ftypes))
            else:
                r = filedialog.askopenfilename(parent=root,
                                               filetypes=ftypes)
                out["paths"] = [r] if r else []
            root.destroy()
        except Exception as e:  # noqa: BLE001
            out["error"] = e

    t = threading.Thread(target=dlg, daemon=True)
    t.start()
    t.join()
    if "error" in out:
        raise ApiError(f"无法打开文件选择框：{out['error']}", 500)
    return [os.path.normpath(p) for p in out.get("paths", [])]


def _explorer_reveal(path: str) -> None:
    """在资源管理器里选中文件。"""
    subprocess.Popen(["explorer", f"/select,{path}"])


class Host:
    """应用状态：工作目录、任务系统、token、可注入的系统交互。"""

    def __init__(self, workdir: Path, jobs: Jobs | None = None,
                 sources_fn=None):
        self.workdir = Path(workdir)
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.config_error = None
        try:
            cfg = paths.load_config()
        except Exception as e:  # noqa: BLE001 - 坏了也要能起界面
            cfg = None
            self.config_error = str(e)
        self._sources_fn = sources_fn or \
            (lambda: sources.configured(paths.load_config()))
        if jobs is None:
            jobs = Jobs(paths.home(), self.workdir,
                        max_parallel=cfg.max_parallel_jobs if cfg else 1)
        self.jobs = jobs
        self.pick = _tk_pick
        self.reveal = _explorer_reveal

    def tool_manifest(self, tool_id: str) -> dict:
        entry = catalog.get(tool_id)
        try:
            inst = store.installed(tool_id)
        except Exception:  # noqa: BLE001 - 本地清单坏了就查源
            inst = None
        if inst is not None:
            return {"id": tool_id, "installed": inst.version,
                    "manifest": inst.manifest.raw}
        errors = []
        for s in self._sources_fn():
            try:
                raw = s.latest_manifest(entry)
            except SourceError as e:
                errors.append(str(e))
                continue
            if raw is not None:
                try:
                    m = json.loads(raw)
                except ValueError as e:
                    raise ApiError(f"清单不是合法 JSON：{e}", 502) from e
                return {"id": tool_id, "installed": None, "manifest": m}
        detail = f"（{'；'.join(errors)}）" if errors else ""
        raise ApiError(f"源上没有 {tool_id} 的清单{detail}", 404)

    def state(self) -> dict:
        try:
            labels = [s.label for s in self._sources_fn()]
            err = self.config_error
        except Exception as e:  # noqa: BLE001
            labels = []
            err = self.config_error or str(e)
        tools = []
        for e in catalog.entries():
            try:
                inst = store.installed(e.id)
            except Exception:  # noqa: BLE001 - 单个工具坏了不拖垮全部
                inst = None
            tools.append({"id": e.id, "title": e.title,
                          "summary": e.summary, "domain": e.domain,
                          "installed": inst.version if inst else None})
        d = {"version": perfbox.__version__, "workdir": str(self.workdir),
             "sources": labels, "domains": _DOMAINS, "tools": tools}
        if err:
            d["config_error"] = err
        return d

    def submit_job(self, body: dict):
        tool_id = body.get("tool")
        command = body.get("command")
        if not isinstance(tool_id, str) or not tool_id:
            raise ApiError("缺少 tool")
        if not isinstance(command, str) or not command:
            raise ApiError("缺少 command")
        values = body.get("values", {})
        if not isinstance(values, dict):
            raise ApiError("values 必须是参数对象")
        out_dir = body.get("out_dir")
        if out_dir is not None and not isinstance(out_dir, str):
            raise ApiError("out_dir 必须是字符串")
        try:
            entry = catalog.get(tool_id)
            inst = store.installed(tool_id)
            if inst is None:
                inst = store.install(entry, self._sources_fn())
            job = self.jobs.start_job(inst, command, values, out_dir)
        except _SUBMIT_ERRORS as e:
            raise ApiError(str(e)) from e
        return job.to_dict()

    def pick_paths(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in ("file", "dir"):
            raise ApiError("kind 必须是 file 或 dir")
        exts = body.get("extensions")
        if exts is not None:
            if not isinstance(exts, list) or any(
                    not isinstance(e, str) or not e.startswith(".")
                    for e in exts):
                raise ApiError("extensions 必须是以 . 开头的字符串数组")
        multiple = bool(body.get("multiple"))
        return {"paths": self.pick(kind, multiple, exts)}

    def reveal_path(self, body: dict) -> dict:
        job = body.get("job")
        rel = body.get("path")
        if not isinstance(job, str) or not isinstance(rel, str):
            raise ApiError("需要 job 和 path")
        j = self._job(job)
        p = self._resolve_in(j.out_dir, rel)
        if p is None:
            raise ApiError("路径在结果目录之外", 400)
        if not p.exists():
            raise ApiError(f"文件不存在：{rel}", 404)
        self.reveal(str(p))
        return {"ok": True}

    def _job(self, job_id: str):
        try:
            return self.jobs.get(job_id)
        except JobNotFound as e:
            raise ApiError(str(e), 404) from e

    @staticmethod
    def _resolve_in(base, rel):
        """把相对路径解析进结果目录；越界返回 None。"""
        try:
            b = Path(base).resolve()
            p = (b / rel).resolve()
            inside = os.path.commonpath(
                [os.path.normcase(str(b)),
                 os.path.normcase(str(p))]) == os.path.normcase(str(b))
        except (OSError, ValueError):
            return None
        return p if inside else None


# --------------------------------------------------------------- handler
def make_handler(host: Host):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"perfbox/{perfbox.__version__}"

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------- helpers
        def _headers(self, ctype, length=None):
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send(self, status, body: bytes, ctype):
            self.send_response(status)
            self._headers(ctype, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False)
                       .encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            h = (self.headers.get("Host") or "").lower()
            return h in (f"127.0.0.1:{host.port}",
                         f"localhost:{host.port}")

        def _authorized(self) -> bool:
            tok = self.headers.get("X-Perfbox-Token") or ""
            return self._host_ok() and secrets.compare_digest(
                tok.encode(), host.token.encode())

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError("请求体过大", 413)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                obj = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError("请求不是有效的 JSON") from None
            if not isinstance(obj, dict):
                raise ApiError("请求必须是 JSON 对象")
            return obj

        # --------------------------------------------------- verbs
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/"):
                return self._api_get(path)
            if path.startswith("/files/"):
                return self._files(path)
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            name = "index.html" if path in ("/", "/index.html") \
                else path.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            self._send(200, f.read_bytes(),
                       _STATIC_TYPES.get(f.suffix.lower(),
                                         "application/octet-stream"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            if not self._authorized():
                return self._json(403, {"error": "未授权的请求"})
            try:
                self._post(path)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": f"内部错误：{e}"})

        def _post(self, path):
            if path == "/api/jobs":
                return self._json(200, host.submit_job(self._body()))
            if path == "/api/pick":
                return self._json(200, host.pick_paths(self._body()))
            if path == "/api/reveal":
                return self._json(200, host.reveal_path(self._body()))
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path[len("/api/jobs/"):-len("/cancel")]
                job = host._job(jid)
                host.jobs.cancel(jid)
                return self._json(200, job.to_dict())
            return self._json(404, {"error": "未知接口"})

        # ------------------------------------------------- api GET
        def _api_get(self, path):
            if not self._authorized():
                return self._json(403, {"error": "未授权的请求"})
            try:
                if path == "/api/state":
                    return self._json(200, host.state())
                if path == "/api/jobs":
                    return self._json(200, {
                        "jobs": [j.to_dict() for j in host.jobs.history()]})
                if path.startswith("/api/jobs/"):
                    jid = path[len("/api/jobs/"):]
                    if "/" in jid:
                        return self._json(404, {"error": "未知接口"})
                    job = host._job(jid)
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    try:
                        offset = int(q.get("log_offset", ["0"])[0])
                    except ValueError:
                        raise ApiError("log_offset 必须是整数") from None
                    text, off = host.jobs.read_log(jid, offset)
                    d = job.to_dict()
                    d["log"] = text
                    d["log_offset"] = off
                    return self._json(200, d)
                if path.startswith("/api/tools/"):
                    tid = path[len("/api/tools/"):]
                    try:
                        return self._json(200, host.tool_manifest(tid))
                    except UnknownTool as e:
                        raise ApiError(str(e), 404) from e
                return self._json(404, {"error": "未知接口"})
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": f"内部错误：{e}"})

        # --------------------------------------------------- files
        def _files(self, path):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            parts = path[len("/files/"):].split("/", 2)
            if len(parts) != 3 or not secrets.compare_digest(
                    parts[0].encode(), host.token.encode()):
                return self._json(403, {"error": "未授权的请求"})
            try:
                job = host._job(parts[1])
            except ApiError:
                return self._send(404, b"not found", "text/plain")
            rel = urllib.parse.unquote(parts[2])
            p = host._resolve_in(job.out_dir, rel)
            if p is None:
                return self._send(403, b"forbidden", "text/plain")
            if not p.is_file():
                return self._send(404, b"not found", "text/plain")
            suffix = p.suffix.lower()
            ctype = _FILE_TYPES.get(suffix, "application/octet-stream")
            size = p.stat().st_size
            self.send_response(200)
            self._headers(ctype, size)
            if suffix == ".xlsx":
                self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            with open(p, "rb") as f:
                while chunk := f.read(1 << 16):
                    self.wfile.write(chunk)

    return Handler


def create_server(port: int = 0, host: Host | None = None):
    """-> (server, host)；已绑定 127.0.0.1，尚未 serve。"""
    host = host or Host(Path.cwd())
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(host))
    srv.daemon_threads = True
    host.port = srv.server_address[1]
    return srv, host


def serve(port: int = 0, open_browser: bool = True) -> int:
    srv, host = create_server(port)
    url = f"http://127.0.0.1:{host.port}/#t={host.token}"
    print(f"perfbox {perfbox.__version__} 已启动：{url}", flush=True)
    print("关闭此窗口即退出。", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        host.jobs.shutdown()
    return 0
