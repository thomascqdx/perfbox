"""测试共享助手：按目录源布局发布假工具（artifact + 合法清单）。"""

import hashlib
import io
import json
import zipfile
from pathlib import Path

from perfbox.catalog import CatalogEntry


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def make_manifest(tool_id="memscope", version="1.0.0", name=None, size=0,
                  sha256="0" * 64, kind="exe", entry=None, **extra):
    name = name or f"{tool_id}-{version}.exe"
    artifact = {"name": name, "kind": kind, "size": size, "sha256": sha256}
    if entry is not None:
        artifact["entry"] = entry
    d = {
        "schema": 1,
        "id": tool_id,
        "title": f"{tool_id} 工具",
        "summary": "测试用假工具",
        "domain": "memory",
        "version": version,
        "artifact": artifact,
        "commands": [{
            "id": "run", "title": "运行", "kind": "job",
            "output": {"flag": "-o", "kind": "dir"},
            "outputs": [{"glob": "*.txt", "role": "report"}],
        }],
    }
    d.update(extra)
    return d


def publish(root, tool_id="memscope", version="1.0.0",
            body=b"fake-tool-bytes", kind="exe", name=None, entry=None,
            manifest_extra=None):
    """在 root 下按目录源布局写 <id>/<version>/<artifact> 与 perfbox-tool.json。

    返回清单 dict（size/sha256 按实际产物计算）。
    """
    d = Path(root) / tool_id / version
    d.mkdir(parents=True, exist_ok=True)
    name = name or f"{tool_id}-{version}.{'zip' if kind == 'zip' else 'exe'}"
    (d / name).write_bytes(body)
    m = make_manifest(tool_id, version, name=name, size=len(body),
                      sha256=hashlib.sha256(body).hexdigest(),
                      kind=kind, entry=entry)
    if manifest_extra:
        m.update(manifest_extra)
    (d / "perfbox-tool.json").write_text(
        json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def entry_of(tool_id="memscope", github="o/r"):
    return CatalogEntry(id=tool_id, title="t", summary="s", domain="memory",
                        github=github)
