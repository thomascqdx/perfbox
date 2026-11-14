import json
import os
import shutil
from pathlib import Path

import pytest

from perfbox import store
from perfbox.manifest import IncompatibleManifest, parse
from perfbox.sources import DirSource, SourceError
from perfbox.store import ChecksumError, StoreError

from helpers import entry_of, make_zip, publish


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("PERFBOX_HOME", str(home))
    return home, src


def tdir(home, tool_id="memscope"):
    return home / "tools" / tool_id


def current_of(home, tool_id="memscope"):
    f = tdir(home, tool_id) / "current.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


class TestInstall:
    def test_install_exe(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        inst = store.install(entry_of(), [DirSource(src)])
        assert inst.version == "1.0.0"
        vdir = tdir(home) / "1.0.0"
        assert (vdir / "memscope-1.0.0.exe").read_bytes() == b"EXE"
        assert (vdir / "perfbox-tool.json").exists()
        assert current_of(home) == {"version": "1.0.0", "previous": None}
        assert inst.entry() == [str(vdir / "memscope-1.0.0.exe")]
        assert not list((home / "downloads").glob("*.part"))

    def test_install_zip_strips_top_dir(self, env):
        home, src = env
        z = make_zip({"pkg-1.0.0/bin/tool.exe": b"Z",
                      "pkg-1.0.0/data/x.dat": b"d"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["bin/tool.exe"])
        inst = store.install(entry_of(), [DirSource(src)])
        vdir = tdir(home) / "1.0.0"
        assert (vdir / "bin" / "tool.exe").read_bytes() == b"Z"
        assert (vdir / "data" / "x.dat").read_bytes() == b"d"
        assert not (vdir / "pkg-1.0.0").exists()

    def test_install_zip_keeps_multiple_top_entries(self, env):
        home, src = env
        z = make_zip({"bin/tool.exe": b"Z", "data/x.dat": b"d"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["bin/tool.exe"])
        store.install(entry_of(), [DirSource(src)])
        vdir = tdir(home) / "1.0.0"
        assert (vdir / "bin" / "tool.exe").exists()
        assert (vdir / "data" / "x.dat").exists()

    @pytest.mark.parametrize("member", ["../evil.txt", "..\\evil.txt",
                                        "C:/abs.txt", "/abs.txt"])
    def test_zip_traversal_rejected_no_residue(self, env, member):
        home, src = env
        z = make_zip({"ok.exe": b"Z", member: b"bad"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["ok.exe"])
        with pytest.raises(StoreError):
            store.install(entry_of(), [DirSource(src)])
        assert not tdir(home).exists() or not list(tdir(home).iterdir())
        dl = home / "downloads"
        assert not dl.exists() or not list(dl.iterdir())

    def test_checksum_mismatch_keeps_previous(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"good")
        store.install(entry_of(), [DirSource(src)])
        old = (tdir(home) / "1.0.0" / "memscope-1.0.0.exe").read_bytes()
        m = publish(src, "memscope", "1.1.0", body=b"new")
        (src / "memscope" / "1.1.0" / m["artifact"]["name"]).write_bytes(
            b"tampered!")
        with pytest.raises(ChecksumError) as ei:
            store.install(entry_of(), [DirSource(src)])
        assert "校验不符" in str(ei.value)
        assert current_of(home)["version"] == "1.0.0"
        assert (tdir(home) / "1.0.0" / "memscope-1.0.0.exe").read_bytes() == old
        assert not list((home / "downloads").glob("*.part"))

    def test_size_mismatch(self, env):
        home, src = env
        m = publish(src, "memscope", "1.0.0", body=b"short")
        (src / "memscope" / "1.0.0" / m["artifact"]["name"]).write_bytes(
            b"short!!")
        with pytest.raises(ChecksumError):
            store.install(entry_of(), [DirSource(src)])

    def test_incomplete_version_dir_reinstalled(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        # 残留的不完整版本目录（清单被删掉、产物还在）
        vdir = tdir(home) / "1.0.0"
        vdir.mkdir(parents=True)
        (vdir / "memscope-1.0.0.exe").write_bytes(b"partial")
        inst = store.install(entry_of(), [DirSource(src)])
        assert inst.version == "1.0.0"
        assert (vdir / "perfbox-tool.json").exists()
        assert (vdir / "memscope-1.0.0.exe").read_bytes() == b"EXE"

    def test_zip_install_keeps_original(self, env):
        home, src = env
        z = make_zip({"pkg/tool.exe": b"Z", "pkg/x.dat": b"d"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["tool.exe"])
        inst = store.install(entry_of(), [DirSource(src)])
        kept = inst.root / ".artifact" / "memscope-1.0.0.zip"
        assert kept.read_bytes() == z

    def test_zip_top_dir_child_same_name(self, env):
        home, src = env
        z = make_zip({"pkg/pkg/tool.exe": b"Z", "pkg/readme.txt": b"r"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["pkg/tool.exe"])
        store.install(entry_of(), [DirSource(src)])
        vdir = tdir(home) / "1.0.0"
        assert (vdir / "pkg" / "tool.exe").read_bytes() == b"Z"
        assert (vdir / "readme.txt").read_bytes() == b"r"

    def test_reinstall_same_version_no_download(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        inst1 = store.install(entry_of(), [DirSource(src)])
        # 删掉源上的产物（保留清单），再次安装不应触发下载
        for f in (src / "memscope" / "1.0.0").iterdir():
            if f.name != "perfbox-tool.json":
                f.unlink()
        inst2 = store.install(entry_of(), [DirSource(src)])
        assert inst2 == inst1 and inst2.version == "1.0.0"

    def test_update_keeps_previous_and_prunes(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0", body=b"v2")
        store.install(entry_of(), [DirSource(src)])
        assert current_of(home) == {"version": "1.1.0", "previous": "1.0.0"}
        publish(src, "memscope", "1.2.0", body=b"v3")
        store.install(entry_of(), [DirSource(src)])
        assert current_of(home) == {"version": "1.2.0", "previous": "1.1.0"}
        assert not (tdir(home) / "1.0.0").exists()
        assert (tdir(home) / "1.1.0").exists()

    def test_in_use_version_not_pruned(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        inst1 = store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0", body=b"v2")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.2.0", body=b"v3")
        with store.use(inst1):
            store.install(entry_of(), [DirSource(src)])
            assert (tdir(home) / "1.0.0").exists()
        assert store.installed("memscope").version == "1.2.0"

    @pytest.mark.parametrize("extra", [{"schema": 2},
                                       {"min_perfbox": "99.0.0"}])
    def test_incompatible_leaves_install_untouched(self, env, extra):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "2.0.0", body=b"v2",
                manifest_extra=extra)
        with pytest.raises(IncompatibleManifest):
            store.install(entry_of(), [DirSource(src)])
        assert current_of(home)["version"] == "1.0.0"
        assert not (tdir(home) / "2.0.0").exists()

    def test_manifest_id_mismatch(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", manifest_extra={"id": "not-memscope"})
        with pytest.raises(StoreError):
            store.install(entry_of(), [DirSource(src)])

    def test_missing_entry_after_extract(self, env):
        home, src = env
        z = make_zip({"data/x.dat": b"d"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["bin/missing.exe"])
        with pytest.raises(StoreError):
            store.install(entry_of(), [DirSource(src)])
        assert not tdir(home).exists() or not list(tdir(home).iterdir())

    def test_source_fallback_order(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        bad = DirSource(src / "nonexistent")
        inst = store.install(entry_of(), [bad, DirSource(src)])
        assert inst.version == "1.0.0"

    def test_no_source_has_tool(self, env):
        home, src = env
        with pytest.raises(StoreError) as ei:
            store.install(entry_of(), [DirSource(src)])
        assert "memscope" in str(ei.value)

    def test_all_sources_fail_reports_errors(self, env):
        home, src = env
        bad = DirSource(src / "nonexistent")
        with pytest.raises(StoreError):
            store.install(entry_of(), [bad])

    def test_install_retries_busy_version_dir(self, env, monkeypatch):
        # 杀软短暂占用刚解压的版本目录：重命名重试后安装成功
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        real_replace = os.replace
        denied = []

        def flaky(src_, dst_):
            if Path(dst_).name == "1.0.0" and len(denied) < 3:
                denied.append(dst_)
                raise PermissionError(5, "拒绝访问")
            return real_replace(src_, dst_)

        monkeypatch.setattr(store.os, "replace", flaky)
        monkeypatch.setattr(store.time, "sleep", lambda *_: None)
        inst = store.install(entry_of(), [DirSource(src)])
        assert inst.version == "1.0.0"
        assert (tdir(home) / "1.0.0" / "perfbox-tool.json").exists()
        assert len(denied) == 3

    def test_install_rename_denied_persistent_cleans_up(self, env,
                                                        monkeypatch):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        real_replace = os.replace

        def denied(src_, dst_):
            if Path(dst_).name == "1.0.0":
                raise PermissionError(5, "拒绝访问")
            return real_replace(src_, dst_)

        monkeypatch.setattr(store.os, "replace", denied)
        monkeypatch.setattr(store.time, "sleep", lambda *_: None)
        with pytest.raises(PermissionError):
            store.install(entry_of(), [DirSource(src)])
        assert not (tdir(home) / "1.0.0").exists()
        assert not (tdir(home) / "1.0.0.tmp").exists()
        assert not list((home / "downloads").glob("*.part"))


class TestInstalled:
    def test_installed_none(self, env):
        assert store.installed("memscope") is None

    def test_installed_returns_current(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        inst = store.installed("memscope")
        assert inst.version == "1.0.0"
        assert inst.id == "memscope"
        assert inst.manifest.id == "memscope"

    def test_entry_resolves_root_placeholder(self, env):
        home, src = env
        z = make_zip({"runtime/python.exe": b"P", "analyze.py": b"a"})
        publish(src, "memscope", "2.1.0", body=z, kind="zip",
                entry=["runtime/python.exe", "-B", "{root}/analyze.py"])
        inst = store.install(entry_of(), [DirSource(src)])
        root = tdir(home) / "2.1.0"
        assert inst.entry() == [str(root / "runtime" / "python.exe"), "-B",
                                f"{root}/analyze.py"]

    def test_argv(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"E",
                manifest_extra={"commands": [{
                    "id": "run", "title": "运行", "kind": "job",
                    "args": ["go"],
                    "output": {"flag": "-o", "kind": "dir"},
                    "outputs": [{"glob": "*.txt", "role": "report"}]}]})
        inst = store.install(entry_of(), [DirSource(src)])
        vdir = tdir(home) / "1.0.0"
        assert inst.argv("run", {}, Path("out")) == [
            str(vdir / "memscope-1.0.0.exe"), "go", "-o", str(Path("out"))]


class TestCheckUpdate:
    def test_update_available(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0")
        assert store.check_update(entry_of(), [DirSource(src)]) == "1.1.0"

    def test_no_update(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        assert store.check_update(entry_of(), [DirSource(src)]) is None

    def test_numeric_not_string_compare(self, env):
        home, src = env
        publish(src, "memscope", "0.10.0")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "0.9.9")
        assert store.check_update(entry_of(), [DirSource(src)]) is None

    def test_not_installed_returns_latest(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        assert store.check_update(entry_of(), [DirSource(src)]) == "1.0.0"

    def test_incompatible_propagates(self, env):
        home, src = env
        publish(src, "memscope", "9.0.0", manifest_extra={"schema": 2})
        with pytest.raises(IncompatibleManifest):
            store.check_update(entry_of(), [DirSource(src)])


class TestRollback:
    def test_rollback_both_directions(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0", body=b"v2")
        store.install(entry_of(), [DirSource(src)])
        inst = store.rollback("memscope")
        assert inst.version == "1.0.0"
        assert current_of(home) == {"version": "1.0.0", "previous": "1.1.0"}
        inst = store.rollback("memscope")
        assert inst.version == "1.1.0"

    def test_rollback_without_previous(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        with pytest.raises(StoreError) as ei:
            store.rollback("memscope")
        assert "没有可回退的版本" in str(ei.value)


class TestRemove:
    def test_remove(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        store.remove("memscope")
        assert not tdir(home).exists()
        assert store.installed("memscope") is None
        store.remove("memscope")  # 未安装时为 no-op

    def test_remove_locked_files(self, env, monkeypatch):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])

        def boom(*a, **k):
            raise PermissionError("文件被占用")

        monkeypatch.setattr(store.shutil, "rmtree", boom)
        with pytest.raises(StoreError) as ei:
            store.remove("memscope")
        assert "未能删除" in str(ei.value)

    def test_remove_in_use_refused(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        inst = store.install(entry_of(), [DirSource(src)])
        with store.use(inst):
            with pytest.raises(StoreError):
                store.remove("memscope")
            assert tdir(home).exists()
        store.remove("memscope")
        assert not tdir(home).exists()

class TestExport:
    def test_export_exe_and_zip(self, env, tmp_path, monkeypatch):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        z = make_zip({"pkg/run.exe": b"R"})
        publish(src, "blkscope", "0.5.0", body=z, kind="zip",
                entry=["run.exe"])
        store.install(entry_of(), [DirSource(src)])
        store.install(entry_of("blkscope"), [DirSource(src)])
        dest = tmp_path / "out-src"
        results = store.export(dest)
        by_id = {r["id"]: r for r in results}
        assert by_id["memscope"]["status"] == "exported"
        assert by_id["blkscope"]["status"] == "exported"
        assert by_id["memscope"]["version"] == "1.0.0"
        # 目录源布局：产物 + 清单逐字节一致
        d = dest / "memscope" / "1.0.0"
        assert (d / "memscope-1.0.0.exe").read_bytes() == b"EXE"
        assert (d / "perfbox-tool.json").read_bytes() == \
            (tdir(home) / "1.0.0" / "perfbox-tool.json").read_bytes()
        assert (dest / "blkscope" / "0.5.0" /
                "blkscope-0.5.0.zip").read_bytes() == z
        # 往返：导出目录直接作为源，在另一个 home 里可装
        monkeypatch.setenv("PERFBOX_HOME", str(tmp_path / "home2"))
        inst = store.install(entry_of("blkscope"), [DirSource(dest)])
        assert (inst.root / "run.exe").read_bytes() == b"R"

    def test_export_unchanged_on_reexport(self, env, tmp_path):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        store.install(entry_of(), [DirSource(src)])
        dest = tmp_path / "out-src"
        store.export(dest)
        results = store.export(dest)
        assert [r["status"] for r in results] == ["unchanged"]

    def test_export_skips_zip_without_original(self, env, tmp_path):
        home, src = env
        z = make_zip({"pkg/run.exe": b"R"})
        publish(src, "memscope", "1.0.0", body=z, kind="zip",
                entry=["run.exe"])
        inst = store.install(entry_of(), [DirSource(src)])
        shutil.rmtree(inst.root / ".artifact")
        r = store.export(tmp_path / "out-src")[0]
        assert r["status"] == "skipped"
        assert r["reason"] == "安装时未保留原始 zip，请卸载后重新安装"

    def test_export_skips_checksum_mismatch(self, env, tmp_path):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        inst = store.install(entry_of(), [DirSource(src)])
        (inst.root / "memscope-1.0.0.exe").write_bytes(b"tampered")
        r = store.export(tmp_path / "out-src")[0]
        assert r["status"] == "skipped"
        assert "校验不符" in r["reason"]

    def test_export_leaves_unrelated_dest_files(self, env, tmp_path):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"EXE")
        store.install(entry_of(), [DirSource(src)])
        dest = tmp_path / "out-src"
        (dest / "memscope" / "1.0.0").mkdir(parents=True)
        keep = dest / "memscope" / "1.0.0" / "notes.txt"
        keep.write_text("留着我", encoding="utf-8")
        (dest / "other.txt").write_text("x", encoding="utf-8")
        store.export(dest)
        assert keep.read_text(encoding="utf-8") == "留着我"
        assert (dest / "other.txt").read_text(encoding="utf-8") == "x"

    def test_export_nothing_installed(self, env, tmp_path):
        assert store.export(tmp_path / "out-src") == []


class TestPreviousVersion:
    def test_none_when_not_installed(self, env):
        home, _ = env
        assert store.previous_version("memscope") is None

    def test_none_after_first_install(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0")
        store.install(entry_of(), [DirSource(src)])
        assert store.previous_version("memscope") is None

    def test_returns_previous_after_update(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0", body=b"v2")
        store.install(entry_of(), [DirSource(src)])
        assert store.previous_version("memscope") == "1.0.0"

    def test_none_when_dir_incomplete(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        publish(src, "memscope", "1.1.0", body=b"v2")
        store.install(entry_of(), [DirSource(src)])
        (tdir(home) / "1.0.0" / "perfbox-tool.json").unlink()
        assert store.previous_version("memscope") is None

    def test_none_when_dir_missing(self, env):
        home, src = env
        publish(src, "memscope", "1.0.0", body=b"v1")
        store.install(entry_of(), [DirSource(src)])
        cur = tdir(home) / "current.json"
        cur.write_text(json.dumps({"version": "1.0.0", "previous": "9.9.9"}),
                       encoding="utf-8")
        assert store.previous_version("memscope") is None
