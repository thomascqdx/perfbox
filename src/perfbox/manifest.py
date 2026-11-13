"""perfbox-tool.json 的解析、校验与命令行拼装（纯函数，不做 I/O）。

规范见 docs/tool-manifest.md（schema 1）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import perfbox

SCHEMA = 1
DOMAINS = ("startup", "jank", "io", "memory", "trace")

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\Z")
_PARAM_TYPES = ("path", "string", "number", "integer", "boolean", "choice")
_TOP_KEYS = {"schema", "id", "title", "summary", "domain", "version", "tag",
             "min_perfbox", "artifact", "commands"}
_ART_KEYS = {"name", "kind", "size", "sha256", "entry"}
_CMD_KEYS = {"id", "title", "summary", "kind", "args", "params",
             "output", "outputs", "ready"}
_OUT_KEYS = {"flag", "kind", "filename", "fresh"}
_SPEC_KEYS = {"glob", "role", "primary"}
_READY_KEYS = {"url_regex"}
_PARAM_KEYS = {"name", "label", "help", "type", "flag", "required",
               "multiple", "count", "default", "advanced", "accept",
               "extensions", "choices"}


class ManifestError(ValueError):
    """清单校验失败；.path 是出错字段的路径，如 commands[0].params[2].accept。"""

    def __init__(self, path: str, reason: str):
        self.path = path
        super().__init__(f"{path}: {reason}" if path else reason)


class IncompatibleManifest(ManifestError):
    """schema 或 min_perfbox 超出宿主能力，提示需要升级 perfbox。"""


class ParamError(ValueError):
    """build_argv 遇到非法参数值；.param 是参数名。"""

    def __init__(self, param: str, msg: str):
        self.param = param
        super().__init__(msg)


def version_tuple(s: str) -> tuple[int, int, int]:
    m = _VER_RE.fullmatch(s) if isinstance(s, str) else None
    if not m:
        raise ValueError(f"不是 X.Y.Z 版本号：{s!r}")
    return (int(m[1]), int(m[2]), int(m[3]))


@dataclass(frozen=True)
class Output:
    flag: str
    kind: str                 # dir | file
    filename: str | None
    fresh: bool = False


@dataclass(frozen=True)
class OutputSpec:
    glob: str
    role: str                 # report | data | trace
    primary: bool = False


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    help: str | None
    type: str
    flag: str | None
    required: bool
    multiple: bool
    count: int | None
    default: object
    advanced: bool
    accept: tuple[str, ...] | None
    extensions: tuple[str, ...] | None
    choices: tuple | None


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    summary: str | None
    kind: str                 # job | app
    args: tuple[str, ...]
    params: tuple[Param, ...]
    output: Output | None
    outputs: tuple[OutputSpec, ...]
    ready_url_regex: str | None


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str                 # exe | zip
    size: int
    sha256: str
    entry: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    schema: int
    id: str
    title: str
    summary: str | None
    domain: str
    version: str
    tag: str
    min_perfbox: str | None
    artifact: Artifact
    commands: tuple[Command, ...]
    raw: dict = field(compare=False)

    def command(self, cid: str) -> Command:
        for c in self.commands:
            if c.id == cid:
                return c
        raise KeyError(cid)


def _fail(path, reason):
    raise ManifestError(path, reason)


def _unknown(d: dict, allowed: set, base: str):
    for k in d:
        if k not in allowed:
            _fail(f"{base}{k}" if base else k, "未定义的字段")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(d, key, path, *, required=True, allow_empty=False):
    v = d.get(key)
    if v is None:
        if required:
            _fail(path, "缺少必填字段")
        return None
    if not isinstance(v, str):
        _fail(path, "不是字符串")
    if not allow_empty and not v:
        _fail(path, "不能为空")
    return v


def _bool(d, key, path, default=False):
    v = d.get(key)
    if v is None:
        return default
    if not isinstance(v, bool):
        _fail(path, "不是布尔值")
    return v


def _parse_artifact(a, path, template):
    if not isinstance(a, dict):
        _fail(path, "不是对象")
    _unknown(a, _ART_KEYS, f"{path}.")
    name = _str(a, "name", f"{path}.name", allow_empty=template)
    if name and ("/" in name or "\\" in name):
        _fail(f"{path}.name", "不能含路径分隔符")
    kind = _str(a, "kind", f"{path}.kind")
    if kind not in ("exe", "zip"):
        _fail(f"{path}.kind", "只能是 exe 或 zip")
    size = a.get("size")
    if not _is_int(size) or size < (0 if template else 1):
        _fail(f"{path}.size", "必须是正整数" if not template else "必须是非负整数")
    sha = _str(a, "sha256", f"{path}.sha256", allow_empty=template)
    if sha and not _SHA_RE.fullmatch(sha):
        _fail(f"{path}.sha256", "必须是 64 位小写十六进制")
    entry = a.get("entry")
    if entry is None:
        if kind == "zip":
            _fail(f"{path}.entry", "zip 产物必填")
        entry = (name,) if name else ()
    else:
        if not isinstance(entry, list) or not entry:
            _fail(f"{path}.entry", "必须是非空数组")
        for i, e in enumerate(entry):
            if not isinstance(e, str) or not e:
                _fail(f"{path}.entry", "元素必须是非空字符串")
        first = entry[0].replace("\\", "/")
        if first.startswith("/") or re.match(r"[A-Za-z]:", first) or \
                ".." in first.split("/"):
            _fail(f"{path}.entry", "入口必须是安装目录内的相对路径")
        if first.lower().endswith((".cmd", ".bat")):
            _fail(f"{path}.entry", "不能用 .cmd 或 .bat 作入口")
        entry = tuple(entry)
    return Artifact(name=name, kind=kind, size=size, sha256=sha, entry=entry)


def _parse_param(p, path):
    if not isinstance(p, dict):
        _fail(path, "不是对象")
    _unknown(p, _PARAM_KEYS, f"{path}.")
    name = _str(p, "name", f"{path}.name")
    label = _str(p, "label", f"{path}.label")
    help_ = _str(p, "help", f"{path}.help", required=False)
    ptype = _str(p, "type", f"{path}.type")
    if ptype not in _PARAM_TYPES:
        _fail(f"{path}.type", f"未知类型：{ptype!r}")
    flag = p.get("flag")
    if flag is not None and (not isinstance(flag, str) or
                             not flag.startswith("-")):
        _fail(f"{path}.flag", "必须以 - 开头")
    required = _bool(p, "required", f"{path}.required")
    multiple = _bool(p, "multiple", f"{path}.multiple")
    advanced = _bool(p, "advanced", f"{path}.advanced")
    count = p.get("count")
    if count is not None and (not _is_int(count) or count < 1):
        _fail(f"{path}.count", "必须是 ≥1 的整数")
    if multiple and count is not None:
        _fail(f"{path}.count", "multiple 与 count 不能同时出现")
    if ptype == "boolean":
        if flag is None:
            _fail(f"{path}.flag", "boolean 必须有 flag")
        if multiple or count is not None:
            _fail(f"{path}.multiple" if multiple else f"{path}.count",
                  "boolean 不能 multiple 或 count")
    accept = p.get("accept")
    extensions = p.get("extensions")
    if ptype == "path":
        if not isinstance(accept, list) or not accept or \
                any(e not in ("file", "dir") for e in accept):
            _fail(f"{path}.accept", "path 需要非空的 file/dir 列表")
        accept = tuple(accept)
    else:
        if accept is not None:
            _fail(f"{path}.accept", "只有 path 可以有 accept")
    if extensions is not None:
        if ptype != "path":
            _fail(f"{path}.extensions", "只有 path 可以有 extensions")
        if not isinstance(extensions, list) or \
                any(not isinstance(e, str) or not e.startswith(".")
                    for e in extensions):
            _fail(f"{path}.extensions", "每个扩展名必须以 . 开头")
        extensions = tuple(extensions)
    choices = p.get("choices")
    if ptype == "choice":
        if not isinstance(choices, list) or not choices:
            _fail(f"{path}.choices", "choice 需要非空 choices")
        choices = tuple(choices)
    elif choices is not None:
        _fail(f"{path}.choices", "只有 choice 可以有 choices")
    return Param(name=name, label=label, help=help_, type=ptype, flag=flag,
                 required=required, multiple=multiple, count=count,
                 default=p.get("default"), advanced=advanced, accept=accept,
                 extensions=extensions, choices=choices)


def _parse_output(o, path):
    if not isinstance(o, dict):
        _fail(path, "不是对象")
    _unknown(o, _OUT_KEYS, f"{path}.")
    flag = _str(o, "flag", f"{path}.flag")
    if not flag.startswith("-"):
        _fail(f"{path}.flag", "必须以 - 开头")
    kind = _str(o, "kind", f"{path}.kind")
    if kind not in ("dir", "file"):
        _fail(f"{path}.kind", "只能是 dir 或 file")
    filename = o.get("filename")
    if kind == "file":
        if not isinstance(filename, str) or not filename or \
                "/" in filename or "\\" in filename:
            _fail(f"{path}.filename", "file 需要不含路径的文件名")
    elif filename is not None:
        _fail(f"{path}.filename", "dir 不能有 filename")
    return Output(flag=flag, kind=kind, filename=filename,
                  fresh=_bool(o, "fresh", f"{path}.fresh"))


def _parse_command(c, path):
    if not isinstance(c, dict):
        _fail(path, "不是对象")
    _unknown(c, _CMD_KEYS, f"{path}.")
    cid = _str(c, "id", f"{path}.id")
    if not _ID_RE.fullmatch(cid):
        _fail(f"{path}.id", "格式应为 [a-z0-9][a-z0-9-]*")
    title = _str(c, "title", f"{path}.title")
    summary = _str(c, "summary", f"{path}.summary", required=False)
    kind = _str(c, "kind", f"{path}.kind")
    if kind not in ("job", "app"):
        _fail(f"{path}.kind", "只能是 job 或 app")
    args = c.get("args", [])
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        _fail(f"{path}.args", "必须是字符串数组")
    params = c.get("params", [])
    if not isinstance(params, list):
        _fail(f"{path}.params", "必须是数组")
    seen = set()
    positionals = []
    plist = []
    for i, p in enumerate(params):
        pr = _parse_param(p, f"{path}.params[{i}]")
        if pr.name in seen:
            _fail(f"{path}.params[{i}].name", "参数名重复")
        seen.add(pr.name)
        plist.append(pr)
        if pr.flag is None:
            positionals.append(pr)
    mult_pos = [p for p in positionals if p.multiple]
    if len(mult_pos) > 1 or (mult_pos and positionals[-1] is not mult_pos[0]):
        _fail(f"{path}.params", "multiple 位置参数至多一个且必须在最后")
    output = None
    outputs = ()
    ready = None
    if kind == "job":
        if c.get("ready") is not None:
            _fail(f"{path}.ready", "job 不能有 ready")
        if c.get("output") is None:
            _fail(f"{path}.output", "job 必须有 output")
        output = _parse_output(c["output"], f"{path}.output")
        olist = c.get("outputs")
        if not isinstance(olist, list) or not olist:
            _fail(f"{path}.outputs", "job 需要非空 outputs")
        primaries = 0
        specs = []
        for i, o in enumerate(olist):
            op = f"{path}.outputs[{i}]"
            if not isinstance(o, dict):
                _fail(op, "不是对象")
            _unknown(o, _SPEC_KEYS, f"{op}.")
            g = _str(o, "glob", f"{op}.glob")
            role = _str(o, "role", f"{op}.role")
            if role not in ("report", "data", "trace"):
                _fail(f"{op}.role", "只能是 report、data 或 trace")
            pri = _bool(o, "primary", f"{op}.primary")
            primaries += pri
            specs.append(OutputSpec(glob=g, role=role, primary=pri))
        if primaries > 1:
            _fail(f"{path}.outputs", "至多一个 primary")
        outputs = tuple(specs)
    else:
        r = c.get("ready")
        if r is None:
            _fail(f"{path}.ready", "app 必须有 ready")
        if not isinstance(r, dict):
            _fail(f"{path}.ready", "不是对象")
        _unknown(r, _READY_KEYS, f"{path}.ready.")
        regex = r.get("url_regex")
        if not isinstance(regex, str):
            _fail(f"{path}.ready.url_regex", "不是字符串")
        try:
            re.compile(regex)
        except re.error as e:
            _fail(f"{path}.ready.url_regex", f"不是合法正则：{e}")
        ready = regex
        if c.get("output") is not None:
            _fail(f"{path}.output", "app 不能有 output")
        if c.get("outputs") is not None:
            _fail(f"{path}.outputs", "app 不能有 outputs")
    return Command(id=cid, title=title, summary=summary, kind=kind,
                   args=tuple(args), params=tuple(plist), output=output,
                   outputs=outputs, ready_url_regex=ready)


def parse(text: str | bytes, *, template: bool = False,
          host_version: str = perfbox.__version__) -> Manifest:
    """解析并严格校验清单。template=True 时 version/tag/artifact 字段允许留空。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ManifestError("", f"不是合法 JSON：{e}") from e
    if not isinstance(d, dict):
        raise ManifestError("", "清单必须是 JSON 对象")

    schema = d.get("schema")
    if not _is_int(schema):
        _fail("schema", "必须是整数")
    if schema > SCHEMA:
        raise IncompatibleManifest(
            "schema", f"需要升级 perfbox：清单 schema {schema}，"
                      f"宿主最高支持 {SCHEMA}")
    if schema < 1:
        _fail("schema", "必须 ≥1")
    mp = d.get("min_perfbox")
    if mp is not None:
        try:
            mpv = version_tuple(mp)
        except ValueError:
            _fail("min_perfbox", "不是 X.Y.Z 版本号")
        if mpv > version_tuple(host_version):
            raise IncompatibleManifest(
                "min_perfbox", f"需要升级 perfbox：清单要求 ≥ {mp}，"
                               f"当前宿主 {host_version}")

    _unknown(d, _TOP_KEYS, "")
    tid = _str(d, "id", "id")
    if not _ID_RE.fullmatch(tid):
        _fail("id", "格式应为 [a-z0-9][a-z0-9-]*")
    title = _str(d, "title", "title")
    summary = _str(d, "summary", "summary", required=False)
    domain = _str(d, "domain", "domain")
    if domain not in DOMAINS:
        _fail("domain", f"只能是 {'、'.join(DOMAINS)} 之一")
    version = _str(d, "version", "version", allow_empty=template)
    if version:
        try:
            version_tuple(version)
        except ValueError:
            _fail("version", "不是 X.Y.Z 版本号")
    tag = _str(d, "tag", "tag", required=False, allow_empty=template)
    if tag is None:
        tag = f"v{version}" if version else ""
    artifact = _parse_artifact(d.get("artifact"), "artifact", template)
    clist = d.get("commands")
    if not isinstance(clist, list) or not clist:
        _fail("commands", "至少需要一个命令")
    seen = set()
    cmds = []
    for i, c in enumerate(clist):
        cmd = _parse_command(c, f"commands[{i}]")
        if cmd.id in seen:
            _fail(f"commands[{i}].id", "命令 id 重复")
        seen.add(cmd.id)
        cmds.append(cmd)
    return Manifest(schema=schema, id=tid, title=title, summary=summary,
                    domain=domain, version=version, tag=tag, min_perfbox=mp,
                    artifact=artifact, commands=tuple(cmds), raw=d)


# ------------------------------------------------------------- build_argv

def _absent(v) -> bool:
    return v is None or v == "" or v == []


def _unchanged(v, p: Param) -> bool:
    if _absent(v):
        return True
    if p.type == "boolean" and v is False:
        return True
    if p.default is not None and v == p.default:
        return True
    return False


def _convert(v, p: Param) -> str:
    label = p.label
    if p.type in ("path", "string"):
        if not isinstance(v, str):
            raise ParamError(p.name, f"{label}：需要字符串")
        return v
    if p.type == "number":
        if isinstance(v, bool):
            raise ParamError(p.name, f"{label}：需要数字")
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, str):
            s = v.strip()
            try:
                float(s)
            except ValueError:
                raise ParamError(p.name, f"{label}：需要数字") from None
            return s
        raise ParamError(p.name, f"{label}：需要数字")
    if p.type == "integer":
        if isinstance(v, bool):
            raise ParamError(p.name, f"{label}：需要整数")
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str):
            s = v.strip()
            try:
                int(s)
            except ValueError:
                raise ParamError(p.name, f"{label}：需要整数") from None
            return s
        raise ParamError(p.name, f"{label}：需要整数")
    if p.type == "choice":
        if v not in p.choices:
            raise ParamError(p.name, f"{label}：只能取 {list(p.choices)} 之一")
        return str(v)
    raise ParamError(p.name, f"{label}：类型 {p.type} 不支持传值")


def _values_of(v, p: Param) -> list[str]:
    if p.multiple or p.count is not None:
        if not isinstance(v, list):
            raise ParamError(p.name, f"{p.label}：需要值列表")
        if p.multiple and not v:
            raise ParamError(p.name, f"{p.label}：至少一个值")
        if p.count is not None and len(v) != p.count:
            raise ParamError(p.name, f"{p.label}：需要恰好 {p.count} 个值")
        return [_convert(x, p) for x in v]
    return [_convert(v, p)]


def build_argv(command: Command, values: dict, out_dir: Path | None,
               port: int | None = None) -> list[str]:
    """按清单拼命令行：args + 位置参数 + 选项参数 + output。不含 entry。"""
    by_name = {p.name: p for p in command.params}
    for k in values:
        if k not in by_name:
            raise ParamError(k, f"未知参数：{k}")
    argv = []
    for a in command.args:
        if "{port}" in a:
            if port is None:
                raise ValueError("args 含 {port} 但未给出 port")
            a = a.replace("{port}", str(port))
        argv.append(a)
    positionals, options = [], []
    for p in command.params:
        if p.name not in values:
            if p.required:
                raise ParamError(p.name, f"{p.label}：必填")
            continue
        v = values[p.name]
        if p.required and _absent(v):
            raise ParamError(p.name, f"{p.label}：必填")
        if not p.required and _unchanged(v, p):
            continue
        if p.type == "boolean":
            if v is True:
                options.append([p.flag])
            elif v is not False:
                raise ParamError(p.name, f"{p.label}：需要布尔值")
            continue
        vals = _values_of(v, p)
        seg = vals if p.flag is None else [p.flag] + vals
        (positionals if p.flag is None else options).append(seg)
    for seg in positionals:
        argv.extend(seg)
    for seg in options:
        argv.extend(seg)
    if command.output is not None:
        if out_dir is None:
            raise ValueError("任务型命令需要 out_dir")
        o = command.output
        target = out_dir if o.kind == "dir" else out_dir / o.filename
        argv.extend([o.flag, str(target)])
    return argv
