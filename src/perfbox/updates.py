"""更新检查：周期性向各源询问最新清单，缓存在 <home>/update-cache.json。

只做查询不做下载；网络错误一律记录为 per-tool error，不抛出。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from perfbox import store
from perfbox.manifest import IncompatibleManifest, ManifestError, parse
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


def check_all(entries, sources, home: Path, *, force: bool = False,
              now=time.time) -> dict:
    """查询每个工具的最新版本。缓存新鲜且未 force 时直接返回缓存。"""
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
    cache = {"checked": now(), "tools": tools}
    write_cache(home, cache)
    return cache
