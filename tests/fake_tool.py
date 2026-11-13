"""行为由参数决定的假工具，供 jobs/server 测试启动为子进程。

用法：python fake_tool.py [k=v,k=v,...] -o <dir>
spec 键：
  lines=N delay=S   逐行打印日志
  utf8=1            打印一行 UTF-8 中文
  gbk=1             打印一行本机 ANSI 代码页编码的中文
  bad=1             打印一行非法字节
  tail=1            打印一行不带换行符的结尾行
  sleep=S           结束前睡眠
  fail=N            以 N 退出
  grandchild=FILE   启动一个睡眠的 python 孙进程并把 pid 写入 FILE
  needfresh=1       -o 目录已存在则以 3 退出
  cwdfile=1         把 os.getcwd() 写进 <out>/cwd.txt
  nofiles=1         不写结果文件
  big=M             结果文件 content.bin 写 M 字节
-o 以 .txt/.html 等扩展名结尾时按“输出到文件”处理：创建父目录并只写该文件。
"""

import locale
import os
import subprocess
import sys
import time


def main():
    args = sys.argv[1:]
    out = None
    if "-o" in args:
        i = args.index("-o")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    spec = {}
    for a in args:
        if a.startswith("-"):
            continue
        for kv in a.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                spec[k] = v
    if out is None:
        print("no -o", file=sys.stderr)
        return 2

    def log(b):
        os.write(1, b)

    def emit(text, enc="utf-8"):
        log(text.encode(enc) + b"\n")

    if spec.get("grandchild"):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"])
        with open(spec["grandchild"], "w") as f:
            f.write(str(p.pid))

    file_mode = bool(os.path.splitext(out)[1])
    if spec.get("needfresh") and os.path.exists(out):
        emit("out dir already exists")
        return 3
    if file_mode:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    else:
        os.makedirs(out, exist_ok=True)

    n = int(spec.get("lines", "0"))
    delay = float(spec.get("delay", "0"))
    for i in range(n):
        emit(f"log line {i}")
        if delay:
            time.sleep(delay)
    if spec.get("utf8"):
        emit("中文日志行", "utf-8")
    if spec.get("gbk"):
        emit("中文日志行", locale.getencoding())
    if spec.get("bad"):
        log(b"invalid \xff\xfe bytes\n")
    if spec.get("tail"):
        log(b"tail-without-newline")

    if spec.get("cwdfile") and not file_mode:
        with open(os.path.join(out, "cwd.txt"), "w") as f:
            f.write(os.getcwd())

    if not spec.get("nofiles"):
        if file_mode:
            with open(out, "wb") as f:
                f.write(b"single-file result\n")
        else:
            with open(os.path.join(out, "report.html"), "w",
                      encoding="utf-8") as f:
                f.write("<html><body>报告</body></html>")
            with open(os.path.join(out, "report.md"), "w",
                      encoding="utf-8") as f:
                f.write("# 报告\n")
            os.makedirs(os.path.join(out, "data"), exist_ok=True)
            with open(os.path.join(out, "data", "data.xlsx"), "wb") as f:
                f.write(b"XLSX")
            with open(os.path.join(out, "中文报告.txt"), "w",
                      encoding="utf-8") as f:
                f.write("中文文件")
            if spec.get("big"):
                with open(os.path.join(out, "content.bin"), "wb") as f:
                    f.write(b"z" * int(spec["big"]))

    if spec.get("sleep"):
        time.sleep(float(spec["sleep"]))
    return int(spec.get("fail", "0"))


if __name__ == "__main__":
    sys.exit(main())
