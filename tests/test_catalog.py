import pytest

from perfbox import catalog
from perfbox.catalog import UnknownTool


def test_entries_contains_memscope_and_perfetto_trim():
    es = catalog.entries()
    assert [e.id for e in es] == ["memscope", "perfetto-trim", "blkscope",
                                "vmscope", "meminfo-diff", "jank-analyzer",
                                "startup-latency"]
    m, p, b, v, mi, j, s = es
    assert m.title == "内存测试包 A/B 对比"
    assert m.summary == "对比两个内存测试包，输出 HTML、Markdown 和 Excel 报告"
    assert m.domain == "memory" and m.github == "thomascqdx/memscope"
    assert p.title == "Trace 裁剪与切分"
    assert p.summary == "按时间裁剪、切分 perfetto trace，或按关键字查找"
    assert p.domain == "trace" and p.github == "thomascqdx/perfetto-trim"
    assert b.title == "块设备 IO 分析"
    assert b.summary == "分析 Perfetto trace 中的块设备 IO，每个 trace 输出一份 HTML 报告"
    assert b.domain == "io" and b.github == "thomascqdx/blkscope"
    assert v.title == "整机内存时序分析"
    assert v.summary == "解析 mobilelog 里的 kernel log 与 events_log，输出整机内存时序报告"
    assert v.domain == "memory" and v.github == "thomascqdx/vmscope"
    assert mi.title == "meminfo 双版本对比"
    assert mi.summary == "对比两个版本的 cat_meminfo 采样，输出单文件 HTML 报告"
    assert mi.domain == "memory" and mi.github == "thomascqdx/meminfo_diff"
    assert j.title == "100ms 卡顿主线程分析"
    assert j.summary == "批量分析 100ms 级卡顿 trace 的主线程耗时，输出 Markdown 报告与 CSV"
    assert j.domain == "jank" and j.github == "thomascqdx/jank-100ms-analysis"
    assert s.title == "启动时延一致性分析"
    assert s.summary == "批量分析启动时延测试表格，定位偏离均值的样本，输出报告与 Excel"
    assert s.domain == "startup" and s.github == "thomascqdx/dynamic_model_tools"


def test_get():
    assert catalog.get("memscope").github == "thomascqdx/memscope"
    with pytest.raises(UnknownTool) as ei:
        catalog.get("nope")
    assert "未知工具：nope" in str(ei.value)
