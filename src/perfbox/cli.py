"""命令行入口：list / install / update / rollback / run / manifest check。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import perfbox
from perfbox import catalog, paths, sources, store
from perfbox.catalog import UnknownTool
from perfbox.manifest import ManifestError, ParamError, parse
from perfbox.paths import ConfigError
from perfbox.sources import SourceError
from perfbox.store import StoreError

_ERRORS = (StoreError, SourceError, ManifestError, UnknownTool, ConfigError,
           ParamError)


def _utf8(stream) -> None:
    if not stream.isatty():
        stream.reconfigure(encoding="utf-8", errors="replace")


def _sources():
    return sources.configured(paths.load_config())


def _progress():
    last = [-1]

    def report(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0] and (pct % 5 == 0 or pct == 100):
            print(f"下载 {pct}%", file=sys.stderr)
            last[0] = pct
    return report


def _cmd_list() -> int:
    srcs = _sources()
    for e in catalog.entries():
        inst = store.installed(e.id)
        line = f"{e.id}\t{e.title}\t{inst.version if inst else '未安装'}"
        try:
            new = store.check_update(e, srcs)
            if new:
                line += f"\t可更新到 {new}"
        except StoreError:
            line += "\t检查失败"
        print(line)
    return 0


def _cmd_install(tool_id: str) -> int:
    entry = catalog.get(tool_id)
    prev = store.installed(tool_id)
    inst = store.install(entry, _sources(), _progress())
    if prev and prev.version == inst.version:
        print(f"{inst.id} {inst.version} 已是最新")
    else:
        print(f"已安装 {inst.id} {inst.version}")
    return 0


def _update_one(entry, srcs) -> None:
    new = store.check_update(entry, srcs)
    if new is None:
        print(f"{entry.id} 已是最新")
    else:
        inst = store.install(entry, srcs, _progress())
        print(f"已更新 {inst.id} {inst.version}")


def _cmd_update(tool_id: str | None, all_: bool) -> int:
    srcs = _sources()
    if all_:
        targets = [e for e in catalog.entries() if store.installed(e.id)]
        if not targets:
            print("没有已安装的工具")
        for e in targets:
            _update_one(e, srcs)
    else:
        _update_one(catalog.get(tool_id), srcs)
    return 0


def _cmd_rollback(tool_id: str) -> int:
    inst = store.rollback(tool_id)
    print(f"{inst.id} 已回退到 {inst.version}")
    return 0


def _cmd_manifest_check(file: str, template: bool) -> int:
    text = Path(file).read_bytes()
    m = parse(text, template=template)
    print(f"OK: {m.id} {m.version}，{len(m.commands)} 个命令")
    return 0


def _run(args: list[str]) -> int:
    if not args:
        print("用法：perfbox run <tool> [--] <传给工具的参数>",
              file=sys.stderr)
        return 1
    tool_id = args[0]
    rest = args[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    inst = store.installed(tool_id)
    if inst is None:
        inst = store.install(catalog.get(tool_id), _sources(), _progress())
    with store.use(inst):
        return subprocess.call(inst.entry() + rest)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="perfbox", description="性能分析工具箱：获取、运行、管理各分析工具")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出名录中的工具、已装版本、可用新版")
    pi = sub.add_parser("install", help="安装工具")
    pi.add_argument("tool")
    pu = sub.add_parser("update", help="更新工具")
    g = pu.add_mutually_exclusive_group(required=True)
    g.add_argument("tool", nargs="?")
    g.add_argument("--all", action="store_true", help="更新全部已装工具")
    pr = sub.add_parser("rollback", help="回退到上一版")
    pr.add_argument("tool")
    sub.add_parser("run", help="运行工具，参数原样传递")
    pm = sub.add_parser("manifest", help="清单操作")
    msub = pm.add_subparsers(dest="mcmd", required=True)
    mc = msub.add_parser("check", help="校验一份清单")
    mc.add_argument("--template", action="store_true",
                    help="按清单模板校验（允许版本和产物字段留空）")
    mc.add_argument("file")
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8(sys.stdout)
    _utf8(sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        if not args:
            parser.print_help()
            return 0
        if args[0] in ("-V", "--version"):
            print(f"perfbox {perfbox.__version__}")
            return 0
        if args[0] == "run":
            return _run(args[1:])
        ns = parser.parse_args(args)
        if ns.cmd == "list":
            return _cmd_list()
        if ns.cmd == "install":
            return _cmd_install(ns.tool)
        if ns.cmd == "update":
            return _cmd_update(ns.tool, ns.all)
        if ns.cmd == "rollback":
            return _cmd_rollback(ns.tool)
        if ns.cmd == "manifest" and ns.mcmd == "check":
            return _cmd_manifest_check(ns.file, ns.template)
        parser.print_help()
        return 0
    except _ERRORS as e:
        print(f"perfbox: 错误：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"perfbox: 错误：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
