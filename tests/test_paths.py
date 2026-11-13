import json
import os
from pathlib import Path

import pytest

from perfbox import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "pbhome"
    monkeypatch.setenv("PERFBOX_HOME", str(h))
    return h


def test_home_from_env(home):
    assert paths.home() == home


def test_home_default_localappdata(monkeypatch):
    monkeypatch.delenv("PERFBOX_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert paths.home() == Path(r"C:\Users\x\AppData\Local\perfbox")


def test_home_fallback_without_localappdata(monkeypatch):
    monkeypatch.delenv("PERFBOX_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert paths.home() == Path.home() / "AppData" / "Local" / "perfbox"


def test_home_does_not_create(home):
    paths.home()
    assert not home.exists()


def test_load_config_missing_file_defaults(home):
    cfg = paths.load_config()
    assert cfg.sources == [{"type": "github"}]
    assert cfg.proxy is None
    assert cfg.max_parallel_jobs == 1


def test_load_config_valid(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({
        "sources": [{"type": "dir", "path": r"\\share\tools"}, {"type": "github"}],
        "proxy": "http://127.0.0.1:7890",
        "max_parallel_jobs": 2,
    }), encoding="utf-8")
    cfg = paths.load_config()
    assert cfg.sources == [{"type": "dir", "path": r"\\share\tools"}, {"type": "github"}]
    assert cfg.proxy == "http://127.0.0.1:7890"
    assert cfg.max_parallel_jobs == 2


def test_load_config_missing_keys_default(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    cfg = paths.load_config()
    assert cfg.sources == [{"type": "github"}]
    assert cfg.proxy is None
    assert cfg.max_parallel_jobs == 1


@pytest.mark.parametrize("text", [
    "not json",
    "[1, 2]",
    json.dumps({"sources": [{"type": "rsync"}]}),
    json.dumps({"sources": [{"type": "dir"}]}),
    json.dumps({"sources": [{"type": "dir", "path": 3}]}),
    json.dumps({"sources": "github"}),
    json.dumps({"max_parallel_jobs": 0}),
    json.dumps({"max_parallel_jobs": "2"}),
    json.dumps({"max_parallel_jobs": True}),
    json.dumps({"proxy": 3}),
])
def test_load_config_invalid(home, text):
    home.mkdir(parents=True)
    (home / "config.json").write_text(text, encoding="utf-8")
    with pytest.raises(paths.ConfigError) as ei:
        paths.load_config()
    assert "config.json" in str(ei.value)


def test_save_config_roundtrip(home):
    cfg = paths.Config(
        sources=[{"type": "dir", "path": "D:\\工具"}],
        proxy="http://127.0.0.1:7890",
        max_parallel_jobs=3,
    )
    paths.save_config(cfg)
    assert (home / "config.json").exists()
    text = (home / "config.json").read_text(encoding="utf-8")
    assert "工具" in text  # ensure_ascii=False
    cfg2 = paths.load_config()
    assert cfg2 == cfg
