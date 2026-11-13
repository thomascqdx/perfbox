import pytest

from perfbox import catalog
from perfbox.catalog import UnknownTool


def test_entries_contains_memscope_and_perfetto_trim():
    es = catalog.entries()
    assert [e.id for e in es] == ["memscope", "perfetto-trim"]
    m, p = es
    assert m.title == "内存测试包 A/B 对比"
    assert m.summary == "对比两个内存测试包，输出 HTML、Markdown 和 Excel 报告"
    assert m.domain == "memory" and m.github == "thomascqdx/memscope"
    assert p.title == "Trace 裁剪与切分"
    assert p.summary == "按时间裁剪、切分 perfetto trace，或按关键字查找"
    assert p.domain == "trace" and p.github == "thomascqdx/perfetto-trim"


def test_get():
    assert catalog.get("memscope").github == "thomascqdx/memscope"
    with pytest.raises(UnknownTool) as ei:
        catalog.get("nope")
    assert "未知工具：nope" in str(ei.value)
