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

应用模式：python fake_tool.py app --port <n> --mode <m>
  gbk       打印一行 ANSI 编码的就绪行后每 50ms 一行 tick，常驻
  flood     就绪行后每 10ms 一行 1KiB，验证宿主持续排空管道
  silent    永不打印就绪行（测试超时）
  die       打印一行错误后以 7 退出
  shortlive 就绪行后打印一行即退出（测试退出检测）
"""

import locale
import os
import subprocess
import sys
import time


def app_mode(args):
    port = 0
    mode = "gbk"
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            i += 1

    def emit(b):
        os.write(1, b)

    if mode == "die":
        emit("启动失败：端口被占用".encode(locale.getencoding()) + b"\n")
        return 7
    if mode != "silent":
        line = f"Fake 1.0 已启动：http://127.0.0.1:{port}/#t=abc"
        enc = locale.getencoding() if mode == "gbk" else "utf-8"
        emit(line.encode(enc) + b"\n")
    if mode == "shortlive":
        emit(b"bye\n")
        return 0
    if mode == "flood":
        n = 0
        while True:
            emit(f"{n} ".encode() + b"x" * 1015 + b"\n")
            n += 1
            time.sleep(0.01)
    n = 0
    while True:                       # 常驻，等待宿主结束进程树
        emit(f"app tick {n}\n".encode())
        n += 1
        time.sleep(0.05)


def main():
    args = sys.argv[1:]
    if args and args[0] == "app":
        return app_mode(args[1:])
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
