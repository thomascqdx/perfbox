"""工具名录：有哪些工具、默认去哪里找。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    title: str
    summary: str
    domain: str
    github: str              # "owner/repo"


class UnknownTool(LookupError):
    def __init__(self, tool_id: str):
        super().__init__(f"未知工具：{tool_id}")


def entries() -> list[CatalogEntry]:
    data = json.loads(
        (Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))
    return [CatalogEntry(id=t["id"], title=t["title"], summary=t["summary"],
                         domain=t["domain"], github=t["github"])
            for t in data["tools"]]


def get(tool_id: str) -> CatalogEntry:
    for e in entries():
        if e.id == tool_id:
            return e
    raise UnknownTool(tool_id)
