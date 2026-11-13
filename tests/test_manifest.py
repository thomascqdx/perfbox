import json
from pathlib import Path

import pytest

from perfbox import manifest
from perfbox.manifest import (IncompatibleManifest, ManifestError, ParamError,
                              build_argv, parse, version_tuple)

SHA = "ab12cd34" * 8


def base_manifest(**kw):
    d = {
        "schema": 1,
        "id": "memscope",
        "title": "内存测试包 A/B 对比",
        "summary": "对比两个内存测试包，输出 HTML、Markdown 和 Excel 报告",
        "domain": "memory",
        "version": "0.8.1",
        "artifact": {"name": "memscope-0.8.1.exe", "kind": "exe",
                     "size": 10483503, "sha256": SHA},
        "commands": [{
            "id": "static-compare",
            "title": "静态内存包对比",
            "kind": "job",
            "args": ["static", "compare"],
            "params": [
                {"name": "base", "label": "基线 A", "type": "path",
                 "accept": ["file", "dir"],
                 "extensions": [".zip", ".rar"], "required": True},
                {"name": "target", "label": "对比 B", "type": "path",
                 "accept": ["file", "dir"],
                 "extensions": [".zip", ".rar"], "required": True},
                {"name": "labels", "label": "两侧标签", "type": "string",
                 "flag": "--labels", "count": 2, "advanced": True},
                {"name": "tmpdir", "label": "临时解压目录", "type": "path",
                 "accept": ["dir"], "flag": "--tmpdir", "advanced": True},
            ],
            "output": {"flag": "-o", "kind": "dir"},
            "outputs": [
                {"glob": "**/report.html", "role": "report", "primary": True},
                {"glob": "**/report.md", "role": "report"},
                {"glob": "**/data.xlsx", "role": "data"},
            ],
        }],
    }
    d.update(kw)
    return d


def parse_dict(d, **kw):
    return parse(json.dumps(d), **kw)


# ------------------------------------------------------------- parse: valid

def test_parse_memscope_example():
    m = parse_dict(base_manifest())
    assert m.schema == 1 and m.id == "memscope"
    assert m.title == "内存测试包 A/B 对比"
    assert m.domain == "memory" and m.version == "0.8.1" and m.tag == "v0.8.1"
    assert m.min_perfbox is None
    a = m.artifact
    assert (a.name, a.kind, a.size, a.sha256) == (
        "memscope-0.8.1.exe", "exe", 10483503, SHA)
    assert a.entry == ("memscope-0.8.1.exe",)
    c = m.command("static-compare")
    assert c.kind == "job" and c.args == ("static", "compare")
    assert c.params[0].name == "base" and c.params[0].required is True
    assert c.params[0].accept == ("file", "dir")
    assert c.params[0].extensions == (".zip", ".rar")
    assert c.params[2].count == 2 and c.params[2].flag == "--labels"
    assert c.output.flag == "-o" and c.output.kind == "dir"
    assert c.output.fresh is False and c.output.filename is None
    assert [o.glob for o in c.outputs] == [
        "**/report.html", "**/report.md", "**/data.xlsx"]
    assert [o.primary for o in c.outputs] == [True, False, False]
    with pytest.raises(KeyError):
        m.command("nope")


def test_parse_app_command():
    m = parse_dict(base_manifest(commands=[{
        "id": "ui", "title": "裁剪 / 切分 / 查找", "kind": "app",
        "args": ["ui", "--port", "{port}", "--no-browser"],
        "ready": {"url_regex": r"http://127\.0\.0\.1:\d+/#t=\S+"},
    }]))
    c = m.command("ui")
    assert c.kind == "app" and c.ready_url_regex == r"http://127\.0\.0\.1:\d+/#t=\S+"
    assert c.output is None and c.outputs == ()


def test_parse_zip_artifact():
    m = parse_dict(base_manifest(artifact={
        "name": "jank-analyzer-2.1.0-windows-x64.zip", "kind": "zip",
        "size": 16900000, "sha256": SHA,
        "entry": ["runtime/python.exe", "-B", "-X", "utf8",
                  "{root}/analyze_jank.py"],
    }))
    assert m.artifact.entry == ("runtime/python.exe", "-B", "-X", "utf8",
                                "{root}/analyze_jank.py")


def test_tag_defaults_to_v_version():
    m = parse_dict(base_manifest(version="2.1.0"))
    assert m.tag == "v2.1.0"


def test_parse_bytes_utf8_sig():
    m = parse(json.dumps(base_manifest()).encode("utf-8-sig"))
    assert m.id == "memscope"


def test_min_perfbox_accepted_when_not_newer():
    m = parse_dict(base_manifest(min_perfbox="0.0.0"), host_version="0.1.0")
    assert m.min_perfbox == "0.0.0"


# ------------------------------------------------------------- parse: errors

def err(d, **kw):
    with pytest.raises(ManifestError) as ei:
        parse_dict(d, **kw)
    return ei.value


@pytest.mark.parametrize("text", ["not json", "[1]", "3", '"s"', "null"])
def test_parse_not_an_object(text):
    e = err_raw(text)
    assert e.path == ""


def err_raw(text, **kw):
    with pytest.raises(ManifestError) as ei:
        parse(text, **kw)
    return ei.value


@pytest.mark.parametrize("schema,exc", [
    (2, IncompatibleManifest), (99, IncompatibleManifest),
    (0, ManifestError), (-1, ManifestError),
    ("1", ManifestError), (True, ManifestError), (1.0, ManifestError),
])
def test_schema_gate(schema, exc):
    d = base_manifest(schema=schema)
    with pytest.raises(exc) as ei:
        parse_dict(d)
    if exc is IncompatibleManifest:
        assert "需要升级 perfbox" in str(ei.value)
        assert str(schema) in str(ei.value)


def test_min_perfbox_newer_than_host():
    d = base_manifest(min_perfbox="99.0.0")
    with pytest.raises(IncompatibleManifest) as ei:
        parse_dict(d, host_version="0.1.0")
    assert "需要升级 perfbox" in str(ei.value)
    assert "99.0.0" in str(ei.value) and "0.1.0" in str(ei.value)


@pytest.mark.parametrize("mp", ["1.0", "1.0.0.0", "x", "1.a.0", 1])
def test_min_perfbox_bad_format(mp):
    assert err(base_manifest(min_perfbox=mp)).path == "min_perfbox"


def test_unknown_keys_rejected_after_compat_gate():
    assert err(base_manifest(extra=1)).path == "extra"
    d = base_manifest(schema=2, extra=1)
    with pytest.raises(IncompatibleManifest):
        parse_dict(d)


@pytest.mark.parametrize("path,patch", [
    ("id", {"id": "Bad ID"}),
    ("id", {"id": "-bad"}),
    ("id", {"id": "a_b"}),
    ("id", {"id": ""}),
    ("id", {"id": 3}),
    ("title", {"title": ""}),
    ("summary", {"summary": 3}),
    ("domain", {"domain": "net"}),
    ("version", {"version": "1.0"}),
    ("version", {"version": "1.2.x"}),
    ("version", {"version": "v1.2.3"}),
    ("tag", {"tag": 3}),
    ("artifact", {"artifact": "x"}),
    ("commands", {"commands": []}),
    ("commands", {"commands": "x"}),
])
def test_top_field_errors(path, patch):
    assert err(base_manifest(**patch)).path == path


@pytest.mark.parametrize("path,art", [
    ("artifact.extra", {"extra": 1}),
    ("artifact.name", {"name": ""}),
    ("artifact.name", {"name": "a/b.exe"}),
    ("artifact.name", {"name": "a\\b.exe"}),
    ("artifact.kind", {"kind": "tar"}),
    ("artifact.size", {"size": 0}),
    ("artifact.size", {"size": -5}),
    ("artifact.size", {"size": "100"}),
    ("artifact.size", {"size": True}),
    ("artifact.sha256", {"sha256": "zz"}),
    ("artifact.sha256", {"sha256": "AB" + "0" * 62}),
    ("artifact.entry", {"kind": "zip"}),  # zip requires entry
    ("artifact.entry", {"kind": "zip", "entry": []}),
    ("artifact.entry", {"entry": ""}),
    ("artifact.entry", {"entry": ["a", ""]}),
    ("artifact.entry", {"entry": ["../x.exe"]}),
    ("artifact.entry", {"entry": ["..\\x.exe"]}),
    ("artifact.entry", {"entry": ["C:/x.exe"]}),
    ("artifact.entry", {"entry": ["/x.exe"]}),
    ("artifact.entry", {"entry": ["x.CMD"]}),
    ("artifact.entry", {"entry": ["x.bat"]}),
])
def test_artifact_errors(path, art):
    a = {"name": "t.exe", "kind": "exe", "size": 5, "sha256": SHA}
    a.update(art)
    assert err(base_manifest(artifact=a)).path == path


def _cmd(**kw):
    c = {"id": "c", "title": "c", "kind": "job",
         "output": {"flag": "-o", "kind": "dir"},
         "outputs": [{"glob": "*.html", "role": "report"}]}
    c.update(kw)
    return c


@pytest.mark.parametrize("path,cmd", [
    ("commands[0].extra", _cmd(extra=1)),
    ("commands[0].id", _cmd(id="Bad")),
    ("commands[0].id", _cmd(id="")),
    ("commands[1].id", None),  # duplicate id, patched below
    ("commands[0].title", _cmd(title="")),
    ("commands[0].kind", _cmd(kind="svc")),
    ("commands[0].args", _cmd(args="x")),
    ("commands[0].args", _cmd(args=[1])),
    ("commands[0].output", _cmd(output=None)),
    ("commands[0].output.flag", _cmd(output={"kind": "dir"})),
    ("commands[0].output.flag", _cmd(output={"flag": "o", "kind": "dir"})),
    ("commands[0].output.kind", _cmd(output={"flag": "-o", "kind": "x"})),
    ("commands[0].output.filename",
     _cmd(output={"flag": "-o", "kind": "file"})),
    ("commands[0].output.filename",
     _cmd(output={"flag": "-o", "kind": "file", "filename": "a/b.md"})),
    ("commands[0].output.junk",
     _cmd(output={"flag": "-o", "kind": "file", "filename": "a.md",
                  "junk": 1})),
    ("commands[0].output.fresh",
     _cmd(output={"flag": "-o", "kind": "dir", "fresh": "x"})),
    ("commands[0].outputs", _cmd(outputs=[])),
    ("commands[0].outputs[0].glob", _cmd(outputs=[{"glob": "", "role": "report"}])),
    ("commands[0].outputs[0].role", _cmd(outputs=[{"glob": "g", "role": "x"}])),
    ("commands[0].outputs", _cmd(outputs=[
        {"glob": "a", "role": "report", "primary": True},
        {"glob": "b", "role": "report", "primary": True}])),
    ("commands[0].ready", _cmd(ready={"url_regex": "x"})),
    ("commands[0].ready", _cmd(kind="app")),
    ("commands[0].ready.url_regex",
     _cmd(kind="app", ready={"url_regex": "([bad"})),
    ("commands[0].ready.url_regex", _cmd(kind="app", ready={"url_regex": 3})),
    ("commands[0].output", _cmd(kind="app", ready={"url_regex": "x"})),
    ("commands[0].outputs", _cmd(kind="app", ready={"url_regex": "x"},
                                 output=None)),
])
def test_command_errors(path, cmd):
    if cmd is None:
        cmds = [_cmd(id="dup"), _cmd(id="dup")]
    else:
        cmds = [cmd]
    assert err(base_manifest(commands=cmds)).path == path


def test_app_with_outputs_and_output_rejected():
    c = _cmd(kind="app", ready={"url_regex": "x"})
    assert err(base_manifest(commands=[c])).path == "commands[0].output"
    c = {"id": "a", "title": "a", "kind": "app",
         "ready": {"url_regex": "x"},
         "outputs": [{"glob": "g", "role": "report"}]}
    assert err(base_manifest(commands=[c])).path == "commands[0].outputs"


def _p(**kw):
    p = {"name": "p", "label": "P", "type": "string"}
    p.update(kw)
    return p


@pytest.mark.parametrize("path,param", [
    ("commands[0].params[0].extra", _p(extra=1)),
    ("commands[0].params[0].name", _p(name="")),
    ("commands[0].params[0].label", _p(label="")),
    ("commands[0].params[0].help", _p(help=3)),
    ("commands[0].params[0].type", _p(type="float")),
    ("commands[0].params[0].flag", _p(flag="x")),
    ("commands[0].params[0].flag", _p(flag=3)),
    ("commands[0].params[0].required", _p(required="yes")),
    ("commands[0].params[0].multiple", _p(multiple=1)),
    ("commands[0].params[0].advanced", _p(advanced=1)),
    ("commands[0].params[0].count", _p(count=0)),
    ("commands[0].params[0].count", _p(count="2")),
    ("commands[0].params[0].count", _p(count=2, multiple=True)),
    ("commands[0].params[0].flag", _p(type="boolean")),
    ("commands[0].params[0].multiple",
     _p(type="boolean", flag="--b", multiple=True)),
    ("commands[0].params[0].count",
     _p(type="boolean", flag="--b", count=2)),
    ("commands[0].params[0].accept", _p(type="path")),
    ("commands[0].params[0].accept", _p(type="path", accept=[])),
    ("commands[0].params[0].accept", _p(type="path", accept=["file", "x"])),
    ("commands[0].params[0].accept", _p(accept=["file"])),
    ("commands[0].params[0].extensions", _p(extensions=[".zip"])),
    ("commands[0].params[0].extensions",
     _p(type="path", accept=["file"], extensions=["zip"])),
    ("commands[0].params[0].choices", _p(type="choice")),
    ("commands[0].params[0].choices", _p(type="choice", choices=[])),
    ("commands[0].params[0].choices", _p(choices=["a"])),
])
def test_param_errors(path, param):
    assert err(base_manifest(commands=[_cmd(params=[param])])).path == path


def test_param_name_duplicate():
    cmds = [_cmd(params=[_p(name="dup"), _p(name="dup")])]
    assert err(base_manifest(commands=cmds)).path == "commands[0].params[1].name"


def test_multiple_positional_must_be_last():
    cmds = [_cmd(params=[
        _p(name="a", multiple=True),
        _p(name="b"),
    ])]
    assert err(base_manifest(commands=cmds)).path == "commands[0].params"
    cmds = [_cmd(params=[
        _p(name="a", multiple=True),
        _p(name="b", multiple=True),
    ])]
    assert err(base_manifest(commands=cmds)).path == "commands[0].params"


def test_params_must_be_list():
    assert err(base_manifest(commands=[_cmd(params="x")])).path == \
        "commands[0].params"


# ------------------------------------------------------------- template mode

def _template(**kw):
    d = base_manifest(version="", tag="", artifact={
        "name": "", "kind": "exe", "size": 0, "sha256": ""})
    d.update(kw)
    return d


def test_template_relaxed_fields():
    m = parse_dict(_template(), template=True)
    assert m.version == "" and m.tag == "" and m.artifact.name == ""
    assert m.artifact.entry == ()


def test_template_empty_fields_rejected_without_template():
    assert err(_template()).path == "version"
    assert err(_template(version="1.0.0")).path == "tag"
    assert err(_template(version="1.0.0", tag="v1.0.0")).path == \
        "artifact.name"
    art = {"name": "x.exe", "kind": "exe", "size": 0, "sha256": ""}
    assert err(_template(version="1.0.0", tag="v1.0.0", artifact=art)).path \
        == "artifact.size"
    art = {"name": "x.exe", "kind": "exe", "size": 5, "sha256": ""}
    assert err(_template(version="1.0.0", tag="v1.0.0", artifact=art)).path \
        == "artifact.sha256"


def test_template_zip_still_needs_entry():
    d = _template(artifact={"name": "", "kind": "zip", "size": 0, "sha256": ""})
    assert err(d, template=True).path == "artifact.entry"


def test_template_still_strict_elsewhere():
    assert err(_template(extra=1), template=True).path == "extra"
    assert err(_template(version="x"), template=True).path == "version"
    d = _template(artifact={"name": "a/b", "kind": "exe", "size": 0,
                            "sha256": ""})
    assert err(d, template=True).path == "artifact.name"


def test_version_tuple():
    assert version_tuple("0.8.1") == (0, 8, 1)
    assert version_tuple("10.0.0") > version_tuple("9.9.9")
    for bad in ["1.0", "1.0.0.0", "v1.0.0", "1.-2.0", "a.b.c", "", "1.2.x"]:
        with pytest.raises(ValueError):
            version_tuple(bad)


# ------------------------------------------------------------- build_argv

def cmd_of(m, cid):
    return m.command(cid)


def test_build_argv_memscope_example(tmp_path):
    c = parse_dict(base_manifest()).command("static-compare")
    argv = build_argv(c, {"base": "a.zip", "target": "b.zip",
                          "labels": ["A", "B"], "tmpdir": "T:\\tmp"},
                      Path("out") / "job1")
    assert argv == ["static", "compare", "a.zip", "b.zip",
                    "--labels", "A", "B", "--tmpdir", "T:\\tmp",
                    "-o", str(Path("out") / "job1")]


def test_build_argv_partial_values(tmp_path):
    c = parse_dict(base_manifest()).command("static-compare")
    argv = build_argv(c, {"base": "a.zip", "target": "b.zip"},
                      Path("o"))
    assert argv == ["static", "compare", "a.zip", "b.zip", "-o", str(Path("o"))]


def test_build_argv_port_substitution():
    c = parse_dict(base_manifest(commands=[{
        "id": "ui", "title": "u", "kind": "app",
        "args": ["ui", "--port", "{port}", "--no-browser"],
        "ready": {"url_regex": "x"},
    }])).command("ui")
    assert build_argv(c, {}, None, port=5050) == \
        ["ui", "--port", "5050", "--no-browser"]
    with pytest.raises(ValueError):
        build_argv(c, {}, None)


def test_build_argv_app_ignores_out_dir():
    c = parse_dict(base_manifest(commands=[{
        "id": "ui", "title": "u", "kind": "app",
        "args": ["ui"], "ready": {"url_regex": "x"},
    }])).command("ui")
    assert build_argv(c, {}, Path("zzz"), port=1) == ["ui"]


def test_build_argv_output_forms(tmp_path):
    for out, expect_tail in [
        ({"flag": "-o", "kind": "dir"}, ["-o", str(Path("r"))]),
        ({"flag": "-o", "kind": "dir", "fresh": True}, ["-o", str(Path("r"))]),
        ({"flag": "--output", "kind": "file", "filename": "a.md"},
         ["--output", str(Path("r") / "a.md")]),
    ]:
        c = parse_dict(base_manifest(commands=[_cmd(output=out)])).command("c")
        assert build_argv(c, {}, Path("r"))[-2:] == expect_tail
    with pytest.raises(ValueError):
        build_argv(parse_dict(base_manifest()).command("static-compare"),
                   {"base": "a", "target": "b"}, None)


def _one_param(p, args=None):
    c = _cmd(args=args or [])
    c["params"] = [p]
    return parse_dict(base_manifest(commands=[c])).command("c")


def test_build_argv_unknown_value_key():
    c = _one_param(_p(name="known"))
    with pytest.raises(ParamError) as ei:
        build_argv(c, {"mystery": 1}, Path("r"))
    assert ei.value.param == "mystery"


@pytest.mark.parametrize("val", [None, "", []])
def test_build_argv_unchanged_skipped(val):
    c = _one_param(_p(name="s", flag="--s"))
    assert build_argv(c, {"s": val}, Path("r"))[:-2] == []
    c2 = _one_param(_p(name="s"))
    assert build_argv(c2, {"s": val}, Path("r"))[:-2] == []


def test_build_argv_default_equal_skipped():
    c = _one_param(_p(name="n", type="number", flag="--n", default=0.5))
    assert build_argv(c, {"n": 0.5}, Path("r"))[:-2] == []
    assert build_argv(c, {"n": 0.7}, Path("r"))[:-2] == ["--n", "0.7"]


def test_build_argv_boolean():
    c = _one_param(_p(name="j", type="boolean", flag="--json"))
    assert build_argv(c, {"j": True}, Path("r"))[:-2] == ["--json"]
    assert build_argv(c, {"j": False}, Path("r"))[:-2] == []
    assert build_argv(c, {}, Path("r"))[:-2] == []


def test_build_argv_required():
    c = _one_param(_p(name="r", required=True))
    for val in [None, "", []]:
        with pytest.raises(ParamError) as ei:
            build_argv(c, {"r": val}, Path("r"))
        assert "必填" in str(ei.value) and ei.value.param == "r"
    with pytest.raises(ParamError):
        build_argv(c, {}, Path("r"))
    # 值等于 default 仍要传
    c2 = _one_param(_p(name="r", required=True, default="d"))
    assert build_argv(c2, {"r": "d"}, Path("r"))[:-2] == ["d"]


@pytest.mark.parametrize("ptype,val,expect", [
    ("string", "abc", "abc"),
    ("path", "D:\\x", "D:\\x"),
    ("number", 3, "3"),
    ("number", 0.5, "0.5"),
    ("number", " 0.25 ", "0.25"),
    ("number", "3", "3"),
    ("integer", 7, "7"),
    ("integer", " 42 ", "42"),
    ("integer", "03", "03"),
])
def test_build_argv_value_shapes(ptype, val, expect):
    kw = {"accept": ["file"]} if ptype == "path" else {}
    c = _one_param(_p(name="v", type=ptype, flag="--v", **kw))
    assert build_argv(c, {"v": val}, Path("r"))[:-2] == ["--v", expect]


@pytest.mark.parametrize("ptype,val", [
    ("string", 5),
    ("path", 5),
    ("number", True),
    ("number", "abc"),
    ("number", [1]),
    ("integer", 3.5),
    ("integer", "3.5"),
    ("integer", True),
    ("choice", "zz"),
])
def test_build_argv_bad_values(ptype, val):
    kw = {"accept": ["file"]} if ptype == "path" else {}
    if ptype == "choice":
        kw = {"choices": ["a", "b"]}
        val = "zz"
    c = _one_param(_p(name="v", label="某参数", type=ptype, flag="--v", **kw))
    with pytest.raises(ParamError) as ei:
        build_argv(c, {"v": val}, Path("r"))
    assert "某参数" in str(ei.value) and ei.value.param == "v"


def test_build_argv_choice():
    c = _one_param(_p(name="m", type="choice", flag="--m",
                      choices=["auto", "2.0", "3.0"]))
    assert build_argv(c, {"m": "2.0"}, Path("r"))[:-2] == ["--m", "2.0"]


def test_build_argv_multiple_and_count():
    c = _one_param(_p(name="i", type="string", flag="--i", multiple=True))
    assert build_argv(c, {"i": ["a", "b", "c"]}, Path("r"))[:-2] == \
        ["--i", "a", "b", "c"]
    with pytest.raises(ParamError):
        build_argv(c, {"i": "a"}, Path("r"))
    c2 = _one_param(_p(name="l", type="string", flag="--l", count=2))
    assert build_argv(c2, {"l": ["x", "y"]}, Path("r"))[:-2] == \
        ["--l", "x", "y"]
    for bad in [["x"], ["x", "y", "z"], "x"]:
        with pytest.raises(ParamError):
            build_argv(c2, {"l": bad}, Path("r"))


def test_build_argv_multiple_positional():
    c = _one_param(_p(name="ins", type="path", accept=["file"],
                      required=True, multiple=True))
    assert build_argv(c, {"ins": ["a.zip", "b.zip"]}, Path("r"))[:-2] == \
        ["a.zip", "b.zip"]


def test_build_argv_multiple_type_coercion():
    c = _one_param(_p(name="n", type="integer", flag="--n", multiple=True))
    assert build_argv(c, {"n": [1, " 2 "]}, Path("r"))[:-2] == \
        ["--n", "1", "2"]


def test_build_argv_multiple_choice():
    c = _one_param(_p(name="m", type="choice", flag="--m", multiple=True,
                      choices=["a", "b"]))
    assert build_argv(c, {"m": ["a", "b"]}, Path("r"))[:-2] == ["--m", "a", "b"]
    with pytest.raises(ParamError):
        build_argv(c, {"m": ["a", "x"]}, Path("r"))


def test_build_argv_option_order_after_positionals():
    c = parse_dict(base_manifest(commands=[_cmd(args=["go"], params=[
        _p(name="opt1", flag="--a"),
        _p(name="pos1", required=True),
        _p(name="opt2", flag="--b"),
        _p(name="pos2", required=True),
    ])])).command("c")
    argv = build_argv(c, {"pos1": "P1", "pos2": "P2", "opt1": "A",
                          "opt2": "B"}, Path("r"))
    assert argv[:-2] == ["go", "P1", "P2", "--a", "A", "--b", "B"]


def test_build_argv_multiple_empty_is_unchanged():
    c = _one_param(_p(name="i", flag="--i", multiple=True))
    assert build_argv(c, {"i": []}, Path("r"))[:-2] == []
    c_req = _one_param(_p(name="i", flag="--i", multiple=True, required=True))
    with pytest.raises(ParamError):
        build_argv(c_req, {"i": []}, Path("r"))
