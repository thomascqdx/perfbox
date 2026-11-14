"""工具库：下载、校验、多版本、回退。

布局见 implementation-plan 3.3：
<home>/downloads/*.part、<home>/tools/<id>/current.json、
<home>/tools/<id>/<version>/（perfbox-tool.json + 产物，zip 已解压）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from perfbox import paths
from perfbox.catalog import CatalogEntry
from perfbox.manifest import Manifest, build_argv, parse, version_tuple
from perfbox.sources import Source, SourceError


class StoreError(Exception):
    pass


class ChecksumError(StoreError):
    """产物大小或 sha256 与清单不符。"""


@dataclass(frozen=True)
class InstalledTool:
    id: str
    version: str
    root: Path
    manifest: Manifest

    def entry(self) -> list[str]:
        e = self.manifest.artifact.entry
        return ([str(self.root / e[0])] +
                [x.replace("{root}", str(self.root)) for x in e[1:]])

    def argv(self, command_id: str, values: dict, out_dir: Path | None,
             port: int | None = None) -> list[str]:
        return self.entry() + build_argv(
            self.manifest.command(command_id), values, out_dir, port)


# 正在使用的 (id, version) -> 计数；剪枝和删除都跳过/拒绝在用版本。
_lock = threading.Lock()
_in_use: dict[tuple[str, str], int] = {}


@contextmanager
def use(tool: InstalledTool):
    key = (tool.id, tool.version)
    with _lock:
        _in_use[key] = _in_use.get(key, 0) + 1
    try:
        yield tool
    finally:
        with _lock:
            _in_use[key] -= 1


def _in_use_versions(tool_id: str) -> set[str]:
    with _lock:
        return {v for (i, v), n in _in_use.items() if i == tool_id and n > 0}


def _tools_dir(tool_id: str) -> Path:
    return paths.home() / "tools" / tool_id


def _read_current(tool_id: str) -> dict | None:
    f = _tools_dir(tool_id) / "current.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(
            d.get("version"), str) else None
    except (OSError, ValueError):
        return None


def _write_current(tool_id: str, version: str, previous: str | None) -> None:
    d = _tools_dir(tool_id)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": version, "previous": previous}, f)
        os.replace(tmp, d / "current.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _installed_at(tool_id: str, version: str) -> InstalledTool | None:
    vdir = _tools_dir(tool_id) / version
    mf = vdir / "perfbox-tool.json"
    if not mf.is_file():
        return None
    return InstalledTool(id=tool_id, version=version, root=vdir,
                         manifest=parse(mf.read_bytes()))


def previous_version(tool_id: str) -> str | None:
    """current.json 里的上一版本号；目录不完整（缺清单）时视为没有。"""
    cur = _read_current(tool_id)
    prev = cur.get("previous") if cur else None
    if not isinstance(prev, str) or not prev:
        return None
    if not (_tools_dir(tool_id) / prev / "perfbox-tool.json").is_file():
        return None
    return prev


def installed(tool_id: str) -> InstalledTool | None:
    cur = _read_current(tool_id)
    if cur is None:
        return None
    return _installed_at(tool_id, cur["version"])


def _latest(tool: CatalogEntry,
            sources: list[Source]) -> tuple[Source, bytes]:
    """按顺序问各源要最新清单；返回 (提供清单的源, 清单字节)。"""
    errors = []
    for s in sources:
        try:
            raw = s.latest_manifest(tool)
        except SourceError as e:
            errors.append(str(e))
            continue
        if raw is not None:
            return s, raw
    detail = "；".join(errors)
    raise StoreError(
        f"没有任何源提供 {tool.id}" + (f"（{detail}）" if detail else ""))


def _verify(path: Path, manifest: Manifest) -> None:
    size = path.stat().st_size
    if size != manifest.artifact.size:
        raise ChecksumError(
            f"校验不符：大小 {size}，清单声明 {manifest.artifact.size}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != manifest.artifact.sha256:
        raise ChecksumError(
            f"校验不符：sha256 {h.hexdigest()}，"
            f"清单声明 {manifest.artifact.sha256}")


def _check_member(name: str) -> None:
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"[A-Za-z]:", n) or \
            ".." in n.split("/"):
        raise StoreError(f"zip 含非法成员路径：{name}")


def _extract_zip(part: Path, dest: Path) -> None:
    with zipfile.ZipFile(part) as z:
        for info in z.infolist():
            _check_member(info.filename)
        z.extractall(dest)
    # 全部内容只有一个顶层目录时去掉这一层。先把顶层目录挪出 dest，
    # 避免子项与顶层目录同名时（pkg/pkg/x）移动撞名。
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        top = entries[0]
        staging = Path(tempfile.mkdtemp(
            dir=dest.parent, prefix=f".{dest.name}.strip-"))
        shutil.move(str(top), staging / top.name)
        for child in (staging / top.name).iterdir():
            shutil.move(str(child), dest)
        shutil.rmtree(staging)


def _replace_retry(src: Path, dst: Path,
                   attempts: int = 50, delay: float = 0.2) -> None:
    # 杀软等会短暂占用刚写完的版本目录，PermissionError 时重试约 10 秒
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def _prune(tool_id: str, keep: set[str]) -> None:
    keep |= _in_use_versions(tool_id)
    tdir = _tools_dir(tool_id)
    for sub in tdir.iterdir():
        if sub.is_dir() and sub.name not in keep:
            try:
                shutil.rmtree(sub, ignore_errors=True)
            except OSError:
                pass


def install(tool: CatalogEntry, sources: list[Source],
            progress: Callable[[int, int], None] | None = None
            ) -> InstalledTool:
    src, raw = _latest(tool, sources)
    m = parse(raw)                      # IncompatibleManifest 原样抛出
    if m.id != tool.id:
        raise StoreError(f"清单 id {m.id} 与工具名录 {tool.id} 不一致")
    tdir = _tools_dir(tool.id)
    cur = _read_current(tool.id)
    vdir = tdir / m.version
    if (vdir / "perfbox-tool.json").is_file():
        # 该版本完整在库：只切换当前版本，不重新下载
        if cur and cur["version"] == m.version:
            return _installed_at(tool.id, m.version)
        _write_current(tool.id, m.version,
                       cur["version"] if cur else None)
        return _installed_at(tool.id, m.version)
    if vdir.exists():
        # 不完整的版本目录（如剪枝时被占用的文件拦下）：先清掉再装
        shutil.rmtree(vdir, ignore_errors=True)
        if vdir.exists():
            raise StoreError(f"旧版本目录被占用，无法替换：{vdir}")

    home = paths.home()
    (home / "downloads").mkdir(parents=True, exist_ok=True)
    part = home / "downloads" / f"{tool.id}-{m.version}.part"
    tmp = tdir / f"{m.version}.tmp"
    try:
        src.fetch_artifact(tool, m, part, progress)
        _verify(part, m)
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        if m.artifact.kind == "exe":
            shutil.move(str(part), tmp / m.artifact.name)
        else:
            _extract_zip(part, tmp)
            adir = tmp / ".artifact"
            adir.mkdir()
            shutil.move(str(part), adir / m.artifact.name)
        (tmp / "perfbox-tool.json").write_bytes(raw)
        if not (tmp / m.artifact.entry[0]).exists():
            raise StoreError(
                f"清单入口 {m.artifact.entry[0]} 在产物中不存在")
        _replace_retry(tmp, tdir / m.version)
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _write_current(tool.id, m.version, cur["version"] if cur else None)
    _prune(tool.id, {m.version, cur["version"] if cur else ""})
    return _installed_at(tool.id, m.version)


def _atomic_copy(src: Path, dst: Path) -> None:
    fd, tmp = tempfile.mkstemp(dir=dst.parent,
                             prefix=f".{dst.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f, open(src, "rb") as s:
            shutil.copyfileobj(s, f)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write(dst: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=dst.parent,
                             prefix=f".{dst.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _export_one(inst: InstalledTool, dest: Path) -> dict:
    m = inst.manifest
    name = m.artifact.name
    src = (inst.root / ".artifact" / name if m.artifact.kind == "zip"
           else inst.root / name)
    r = {"id": inst.id, "version": inst.version, "status": "exported"}
    if not src.is_file():
        r["status"] = "skipped"
        r["reason"] = ("安装时未保留原始 zip，请卸载后重新安装"
                       if m.artifact.kind == "zip"
                       else "产物文件缺失，请卸载后重新安装")
        return r
    try:
        _verify(src, m)
    except ChecksumError as e:
        r["status"] = "skipped"
        r["reason"] = str(e)
        return r
    mf_bytes = (inst.root / "perfbox-tool.json").read_bytes()
    vdir = dest / inst.id / inst.version
    d_art = vdir / name
    d_mf = vdir / "perfbox-tool.json"
    try:
        _verify(d_art, m)
        same_art = d_art.is_file()
    except (ChecksumError, OSError):
        same_art = False
    same_mf = d_mf.is_file() and d_mf.read_bytes() == mf_bytes
    if same_art and same_mf:
        r["status"] = "unchanged"
        return r
    vdir.mkdir(parents=True, exist_ok=True)
    if not same_art:
        _atomic_copy(src, d_art)
    if not same_mf:
        _atomic_write(d_mf, mf_bytes)
    return r


def export(dest: Path) -> list[dict]:
    """把全部已装工具的当前版本导出成目录源布局（供共享盘/离线分发）。

    返回每个工具的 {id, version, status, reason?}；dest 里无关内容不动。
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    troot = paths.home() / "tools"
    if troot.is_dir():
        for sub in sorted(troot.iterdir()):
            if not sub.is_dir():
                continue
            inst = installed(sub.name)
            if inst is not None:
                results.append(_export_one(inst, dest))
    return results


def check_update(tool: CatalogEntry, sources: list[Source]) -> str | None:
    """源上有比当前版本更新的版本时返回其版本号。"""
    _, raw = _latest(tool, sources)
    m = parse(raw)
    cur = installed(tool.id)
    if cur is None or version_tuple(m.version) > version_tuple(cur.version):
        return m.version
    return None


def rollback(tool_id: str) -> InstalledTool:
    cur = _read_current(tool_id)
    prev = cur.get("previous") if cur else None
    inst = _installed_at(tool_id, prev) if prev else None
    if inst is None:
        raise StoreError("没有可回退的版本")
    _write_current(tool_id, prev, cur["version"])
    return inst


def remove(tool_id: str) -> None:
    tdir = _tools_dir(tool_id)
    if not tdir.exists():
        return
    if _in_use_versions(tool_id):
        raise StoreError(f"{tool_id} 正在使用中，不能删除")
    try:
        shutil.rmtree(tdir)
    except OSError as e:
        raise StoreError(f"{tool_id} 的部分文件正被占用，未能删除：{e}") from e
