"""源：能提供某个工具的清单和产物的位置（GitHub Release 或目录）。"""

from __future__ import annotations

import http.client
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Protocol

import perfbox
from perfbox.catalog import CatalogEntry
from perfbox.manifest import Manifest, version_tuple
from perfbox.paths import Config

CHUNK = 1 << 20
MAX_MANIFEST = 1 << 20


class SourceError(Exception):
    """源不可达或传输失败（区别于“这个源没有该工具”→ None）。"""


class Source(Protocol):
    label: str

    def latest_manifest(self, tool: CatalogEntry) -> bytes | None: ...
    def fetch_artifact(self, tool: CatalogEntry, manifest: Manifest,
                       dest: Path,
                       progress: Callable[[int, int], None] | None) -> None: ...


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class GitHubSource:
    """GitHub Release 源；base_url 可指向同路径布局的镜像。"""

    def __init__(self, base_url: str = "https://github.com",
                 proxy: str | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = "GitHub"
        handlers = []
        if proxy is not None:
            handlers.append(urllib.request.ProxyHandler(
                {} if proxy == "" else {"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    def _url(self, tool: CatalogEntry, *parts: str) -> str:
        owner, repo = tool.github.split("/", 1)
        path = "/".join([_quote(owner), _quote(repo), "releases",
                         *(_quote(p) for p in parts)])
        return f"{self.base_url}/{path}"

    def _open(self, url: str):
        req = urllib.request.Request(
            url, headers={"User-Agent": f"perfbox/{perfbox.__version__}"})
        return self._opener.open(req, timeout=self.timeout)

    def _get(self, url: str):
        """返回 (status, response)；HTTPError 状态原样返回。"""
        try:
            resp = self._open(url)
            return 200, resp
        except urllib.error.HTTPError as e:
            return e.code, e

    def latest_manifest(self, tool: CatalogEntry) -> bytes | None:
        url = self._url(tool, "latest", "download", "perfbox-tool.json")
        last_err = None
        for attempt in range(2):     # 瞬时失败重试一次
            try:
                status, resp = self._get(url)
            except (OSError, http.client.HTTPException) as e:
                last_err = e
                continue
            if status == 404:
                resp.close()
                return None
            if status == 200:
                try:
                    data = resp.read(MAX_MANIFEST + 1)
                except (OSError, http.client.HTTPException) as e:
                    last_err = e
                    continue
                finally:
                    resp.close()
                if len(data) > MAX_MANIFEST:
                    raise SourceError(f"{url}：清单超过 1 MiB")
                return data
            resp.close()
            if status >= 500:
                last_err = SourceError(f"{url}：HTTP {status}")
                continue
            raise SourceError(f"{url}：HTTP {status}")
        if isinstance(last_err, SourceError):
            raise last_err
        raise SourceError(f"{url}：{last_err}")

    def fetch_artifact(self, tool: CatalogEntry, manifest: Manifest,
                       dest: Path,
                       progress: Callable[[int, int], None] | None) -> None:
        url = self._url(tool, "download", manifest.tag,
                        manifest.artifact.name)
        try:
            status, resp = self._get(url)
        except (OSError, http.client.HTTPException) as e:
            raise SourceError(f"{url}：{e}") from e
        if status != 200:
            resp.close()
            raise SourceError(f"{url}：HTTP {status}")
        declared = resp.headers.get("Content-Length")
        expected = int(declared) if declared and declared.isdigit() else None
        total = expected if expected is not None else manifest.artifact.size
        done = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except (OSError, http.client.HTTPException) as e:
            raise SourceError(f"{url}：{e}") from e
        finally:
            resp.close()
        if done < total:
            raise SourceError(f"{url}：下载不完整（{done}/{total} 字节）")


class DirSource:
    """目录源：<root>/<id>/<version>/perfbox-tool.json + 产物。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.label = str(root)

    def latest_manifest(self, tool: CatalogEntry) -> bytes | None:
        try:
            if not self.root.is_dir():
                raise SourceError(f"目录源不可达：{self.root}")
            tool_dir = self.root / tool.id
            if not tool_dir.is_dir():
                return None
            best = None
            for sub in tool_dir.iterdir():
                if not sub.is_dir():
                    continue
                try:
                    ver = version_tuple(sub.name)
                except ValueError:
                    continue
                if (sub / "perfbox-tool.json").is_file() and \
                        (best is None or ver > best[0]):
                    best = (ver, sub)
            if best is None:
                return None
            return (best[1] / "perfbox-tool.json").read_bytes()
        except SourceError:
            raise
        except OSError as e:
            raise SourceError(f"目录源 {self.root}：{e}") from e

    def fetch_artifact(self, tool: CatalogEntry, manifest: Manifest,
                       dest: Path,
                       progress: Callable[[int, int], None] | None) -> None:
        src = self.root / tool.id / manifest.version / manifest.artifact.name
        try:
            total = src.stat().st_size
            done = 0
            with open(src, "rb") as f, open(dest, "wb") as g:
                while chunk := f.read(CHUNK):
                    g.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except OSError as e:
            raise SourceError(f"{src}：{e}") from e


def configured(config: Config,
               env: Mapping[str, str] | None = None) -> list[Source]:
    """按配置构造源列表；PERFBOX_SOURCE 环境变量优先于配置。"""
    if env is None:
        env = os.environ
    override = env.get("PERFBOX_SOURCE")
    if override:
        return [DirSource(Path(override))]
    result = []
    for s in config.sources:
        if s["type"] == "github":
            result.append(GitHubSource(
                base_url=s.get("base_url") or "https://github.com",
                proxy=config.proxy))
        else:
            result.append(DirSource(Path(s["path"])))
    return result
