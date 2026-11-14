"""更新检查：周期性向各源询问最新清单，缓存在 <home>/update-cache.json。

只做查询不做下载；网络错误一律记录为 per-tool error，不抛出。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import perfbox
from perfbox import store
from perfbox.manifest import IncompatibleManifest, ManifestError, parse, \
    version_tuple
from perfbox.sources import SourceError

TTL = 6 * 3600
_NAME = "update-cache.json"


def read_cache(home: Path) -> dict:
    """{"checked": float, "tools": {id: {...}}}；缺失/损坏返回空缓存。"""
    try:
        d = json.loads((Path(home) / _NAME).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("checked"), (int, float)) \
                and isinstance(d.get("tools"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"checked": 0, "tools": {}}


def write_cache(home: Path, cache: dict) -> None:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=home, prefix="update.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, home / _NAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry_blank() -> dict:
    return {"latest": None, "error": None, "incompatible": None}


def check_host(base_url: str = "https://github.com",
               repo: str = "thomascqdx/perfbox",
               proxy: str | None = None, timeout: float = 15) -> dict:
    """问 releases/latest 的重定向目标，取 tag 里的版本号；永不抛出。"""
    url = f"{base_url.rstrip('/')}/{repo}/releases/latest"
    r = {"latest": None, "url": url, "error": None}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    handlers = [_NoRedirect()]
    if proxy is not None:
        handlers.append(urllib.request.ProxyHandler(
            {} if proxy == "" else {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url, headers={"User-Agent": f"perfbox/{perfbox.__version__}"})
    try:
        resp = opener.open(req, timeout=timeout)
        status, loc = resp.status, resp.headers.get("Location")
        resp.close()
    except urllib.error.HTTPError as e:
        status, loc = e.code, e.headers.get("Location")
        e.close()
    except (OSError, http.client.HTTPException, ValueError) as e:
        r["error"] = str(e)
        return r
    if status in (301, 302, 303, 307) and loc:
        m = re.search(r"/releases/tag/v([^/\s]+)\s*$", loc)
        if m:
            try:
                version_tuple(m.group(1))
                r["latest"] = m.group(1)
            except ValueError:
                pass
    return r


def check_all(entries, sources, home: Path, *, force: bool = False,
              now=time.time, proxy: str | None = None) -> dict:
    """查询每个工具的最新版本，顺带查宿主新版。缓存新鲜且未 force 时直接返回。"""
    home = Path(home)
    cache = read_cache(home)
    if not force and cache["checked"] > 0 \
            and now() - cache["checked"] < TTL:
        return cache
    tools = {}
    for e in entries:
        r = _entry_blank()
        try:
            _, raw = store._latest(e, sources)
            try:
                m = parse(raw)
            except IncompatibleManifest as im:
                r["incompatible"] = str(im)
            except ManifestError as me:
                r["error"] = f"清单无效：{me}"
            else:
                r["latest"] = m.version
        except (SourceError, store.StoreError) as se:
            r["error"] = str(se)
        tools[e.id] = r
    cache = {"checked": now(), "tools": tools,
             "host": check_host(proxy=proxy)}
    write_cache(home, cache)
    return cache
