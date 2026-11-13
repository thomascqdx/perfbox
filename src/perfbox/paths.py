"""宿主数据根目录与 config.json 读写。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    """宿主数据根目录：PERFBOX_HOME 或 %LOCALAPPDATA%\\perfbox。不创建目录。"""
    env = os.environ.get("PERFBOX_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "perfbox"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    sources: list[dict] = field(default_factory=lambda: [{"type": "github"}])
    proxy: str | None = None
    max_parallel_jobs: int = 1


def validate(data) -> Config:
    if not isinstance(data, dict):
        raise ValueError("不是 JSON 对象")
    sources = data.get("sources", [{"type": "github"}])
    if not isinstance(sources, list):
        raise ValueError("sources 不是数组")
    for s in sources:
        if not isinstance(s, dict):
            raise ValueError("sources 元素不是对象")
        t = s.get("type")
        if t not in ("github", "dir"):
            raise ValueError(f"未知的源类型：{t!r}")
        if t == "dir" and not isinstance(s.get("path"), str):
            raise ValueError("dir 源缺少 path")
    proxy = data.get("proxy")
    if proxy is not None and not isinstance(proxy, str):
        raise ValueError("proxy 不是字符串")
    mpj = data.get("max_parallel_jobs", 1)
    if not isinstance(mpj, int) or isinstance(mpj, bool) or mpj < 1:
        raise ValueError("max_parallel_jobs 必须是 ≥1 的整数")
    return Config(sources=sources, proxy=proxy, max_parallel_jobs=mpj)


def load_config() -> Config:
    """读 home()/config.json；缺文件用默认值，内容非法抛 ConfigError。"""
    f = home() / "config.json"
    if not f.exists():
        return Config()
    try:
        return validate(json.loads(f.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        raise ConfigError(f"{f}：{e}") from e


def save_config(cfg: Config) -> None:
    """原子写 home()/config.json。"""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {"sources": cfg.sources, "proxy": cfg.proxy,
         "max_parallel_jobs": cfg.max_parallel_jobs},
        ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=h, prefix="config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, h / "config.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
