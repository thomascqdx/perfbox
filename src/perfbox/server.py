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
from perfbox import catalog, manifest, paths, sources, store, updates
from perfbox.catalog import UnknownTool
from perfbox.jobs import Jobs, JobError, JobNotFound
from perfbox.manifest import (IncompatibleManifest, ManifestError,
                              ParamError, version_tuple)
from perfbox.paths import ConfigError
from perfbox.sources import SourceError
from perfbox.store import StoreError

STATIC = Path(__file__).with_name("static")
MAX_BODY = 1 << 20

_STATIC_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".ico": "image/vnd.microsoft.icon"}

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
                 sources_fn=None, check_updates: bool = True):
        self.workdir = Path(workdir)
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.config_error = None
        try:
            cfg = paths.load_config()
        except Exception as e:  # noqa: BLE001 - 坏了也要能起界面
            cfg = None
            self.config_error = str(e)
        self._cfg = cfg or paths.Config()
        self._sources_fn = sources_fn or \
            (lambda: sources.configured(paths.load_config()))
        if jobs is None:
            jobs = Jobs(paths.home(), self.workdir,
                        max_parallel=cfg.max_parallel_jobs if cfg else 1)
        self.jobs = jobs
        self.pick = _tk_pick
        self.reveal = _explorer_reveal
        self._op_lock = threading.Lock()
        self._ops = {}                    # tool_id -> 操作进度 dict
        self._checking = False
        self._cache = updates.read_cache(paths.home())
        if check_updates:
            self.recheck_updates()

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
                try:
                    manifest.parse(raw)
                except IncompatibleManifest as e:
                    raise ApiError(str(e), 409) from e
                except ManifestError as e:
                    raise ApiError(f"清单无效：{e}", 502) from e
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
        cache = self._cache
        cache_tools = cache.get("tools", {})
        live_apps = {a.tool: a.to_dict() for a in self.jobs.apps()}
        tools = []
        for e in catalog.entries():
            try:
                inst = store.installed(e.id)
            except Exception:  # noqa: BLE001 - 单个工具坏了不拖垮全部
                inst = None
            ce = cache_tools.get(e.id, {})
            latest = ce.get("latest")
            update = None
            if inst is not None and latest:
                try:
                    if version_tuple(latest) > version_tuple(inst.version):
                        update = latest
                except ValueError:
                    pass
            with self._op_lock:
                op = dict(self._ops[e.id]) if e.id in self._ops else None
            tools.append({"id": e.id, "title": e.title,
                          "summary": e.summary, "domain": e.domain,
                          "installed": inst.version if inst else None,
                          "previous": store.previous_version(e.id),
                          "latest": latest, "update": update,
                          "incompatible": ce.get("incompatible"),
                          "op": op,
                          "app": live_apps.get(e.id)})
        host_update = None
        hc = cache.get("host") or {}
        latest = hc.get("latest")
        if latest:
            try:
                if version_tuple(latest) > version_tuple(perfbox.__version__):
                    host_update = {"latest": latest, "url": hc.get("url")}
            except ValueError:
                pass
        d = {"version": perfbox.__version__, "workdir": str(self.workdir),
             "sources": labels, "domains": _DOMAINS, "tools": tools,
             "checked": cache.get("checked", 0),
             "checking": self._checking,
             "host_update": host_update}
        if err:
            d["config_error"] = err
        return d

    # ------------------------------------------------------- 更新检查
    def recheck_updates(self, force: bool = False) -> None:
        """后台跑一次更新检查；已在跑则跳过。"""
        with self._op_lock:
            if self._checking:
                return
            self._checking = True

        def run():
            try:
                self._cache = updates.check_all(
                    catalog.entries(), self._sources_fn(), paths.home(),
                    force=force, proxy=paths.load_config().proxy)
            except Exception:  # noqa: BLE001 - 检查失败不影响界面
                pass
            finally:
                with self._op_lock:
                    self._checking = False

        threading.Thread(target=run, daemon=True,
                         name="perfbox-updates").start()

    def _refresh_cache_entry(self, tool_id: str, version: str) -> None:
        """安装/更新成功后把缓存里的最新版本对齐到已装版本。"""
        home = paths.home()
        cache = updates.read_cache(home)
        e = cache["tools"].setdefault(
            tool_id, {"latest": None, "error": None, "incompatible": None})
        e.update({"latest": version, "error": None, "incompatible": None})
        try:
            updates.write_cache(home, cache)
        except OSError:
            pass
        self._cache = cache

    # ------------------------------------------------------- 安装操作
    def ops(self) -> dict:
        with self._op_lock:
            return {"ops": {t: dict(o) for t, o in self._ops.items()}}

    def start_install_op(self, tool_id: str, kind: str) -> dict:
        """后台安装/更新一个工具；已有未完成操作返回 409。"""
        try:
            entry = catalog.get(tool_id)
        except UnknownTool as e:
            raise ApiError(str(e), 404) from e
        with self._op_lock:
            cur = self._ops.get(tool_id)
            if cur is not None and not cur["finished"]:
                raise ApiError(f"{tool_id} 已有正在进行的操作", 409)
            op = {"kind": kind, "done": 0, "total": 0,
                  "error": None, "finished": False}
            self._ops[tool_id] = op

        def run():
            try:
                inst = store.install(entry, self._sources_fn(),
                                     self._progress(tool_id))
                self._refresh_cache_entry(tool_id, inst.version)
            except Exception as e:  # noqa: BLE001 - 错误记在操作上
                op["error"] = str(e)
            finally:
                op["finished"] = True

        threading.Thread(target=run, daemon=True,
                         name=f"perfbox-op-{tool_id}").start()
        return dict(op)

    def _progress(self, tool_id: str):
        def prog(done, total):
            with self._op_lock:
                op = self._ops.get(tool_id)
                if op is not None:
                    op["done"], op["total"] = done, total
        return prog

    def rollback_tool(self, tool_id: str) -> dict:
        try:
            inst = store.rollback(tool_id)
        except StoreError as e:
            raise ApiError(str(e), 409) from e
        return {"installed": inst.version}

    def remove_tool(self, tool_id: str) -> dict:
        for a in self.jobs.apps():
            if a.tool == tool_id:
                raise ApiError(
                    f"{tool_id} 的应用正在运行，请先停止它再卸载", 409)
        for j in self.jobs.history():
            if j.tool == tool_id and j.state in ("queued", "running"):
                raise ApiError(
                    f"{tool_id} 有任务在排队或运行中，请先停止它再卸载",
                    409)
        try:
            store.remove(tool_id)
        except StoreError as e:
            raise ApiError(str(e), 409) from e
        return {"removed": tool_id}

    # ------------------------------------------------------- 应用
    def start_app(self, body: dict) -> dict:
        tool_id = body.get("tool")
        command = body.get("command")
        if not isinstance(tool_id, str) or not tool_id:
            raise ApiError("缺少 tool")
        if not isinstance(command, str) or not command:
            raise ApiError("缺少 command")
        try:
            entry = catalog.get(tool_id)
            inst = store.installed(tool_id)
            if inst is None:
                inst = store.install(entry, self._sources_fn())
            app = self.jobs.start_app(inst, command)
        except _SUBMIT_ERRORS as e:
            raise ApiError(str(e)) from e
        return app.to_dict()

    # ------------------------------------------------------- 配置
    def get_config(self) -> dict:
        path = str(paths.home() / "config.json")
        try:
            cfg = paths.load_config()
        except ConfigError as e:
            cfg = paths.Config()
            return {"path": path, "sources": cfg.sources,
                    "proxy": cfg.proxy,
                    "max_parallel_jobs": cfg.max_parallel_jobs,
                    "error": str(e)}
        return {"path": path, "sources": cfg.sources, "proxy": cfg.proxy,
                "max_parallel_jobs": cfg.max_parallel_jobs, "error": None}

    def save_config(self, body: dict) -> dict:
        try:
            cur = paths.load_config()
        except ConfigError:
            cur = paths.Config()
        data = {"sources": cur.sources, "proxy": cur.proxy,
                "max_parallel_jobs": cur.max_parallel_jobs}
        for k in data:
            if k in body:
                data[k] = body[k]
        try:
            cfg = paths.validate(data)
        except ValueError as e:
            raise ApiError(f"配置无效：{e}") from e
        paths.save_config(cfg)
        resp = {"path": str(paths.home() / "config.json"),
                "sources": cfg.sources, "proxy": cfg.proxy,
                "max_parallel_jobs": cfg.max_parallel_jobs, "error": None}
        if cfg.max_parallel_jobs != self._cfg.max_parallel_jobs:
            resp["restart_needed"] = True
        if cfg.sources != cur.sources or cfg.proxy != cur.proxy:
            # 源/代理变了：立刻按新配置重查，不等 TTL
            self.recheck_updates(force=True)
            resp["rechecking"] = True
        return resp

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
            if path == "/api/apps":
                return self._json(200, host.start_app(self._body()))
            if path.startswith("/api/apps/") and path.endswith("/stop"):
                tid = urllib.parse.unquote(
                    path[len("/api/apps/"):-len("/stop")])
                host.jobs.stop_app(tid)
                return self._json(200, {"ok": True})
            if path == "/api/config":
                return self._json(200, host.save_config(self._body()))
            if path == "/api/updates/check":
                host.recheck_updates(force=True)
                return self._json(200, {"checking": True})
            if path.startswith("/api/tools/"):
                rest = path[len("/api/tools/"):]
                tid, sep, action = rest.rpartition("/")
                if sep and tid:
                    tid = urllib.parse.unquote(tid)
                    if action == "install":
                        return self._json(
                            202, host.start_install_op(tid, "install"))
                    if action == "update":
                        return self._json(
                            202, host.start_install_op(tid, "update"))
                    if action == "rollback":
                        return self._json(200, host.rollback_tool(tid))
                    if action == "remove":
                        return self._json(200, host.remove_tool(tid))
            return self._json(404, {"error": "未知接口"})

        # ------------------------------------------------- api GET
        def _api_get(self, path):
            if not self._authorized():
                return self._json(403, {"error": "未授权的请求"})
            try:
                if path == "/api/state":
                    return self._json(200, host.state())
                if path == "/api/apps":
                    return self._json(200, {
                        "apps": [a.to_dict() for a in host.jobs.apps()]})
                if path == "/api/ops":
                    return self._json(200, host.ops())
                if path == "/api/config":
                    return self._json(200, host.get_config())
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
