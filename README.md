# perfbox

perfbox 是性能问题分析工具箱的统一入口。它本身不含任何分析逻辑：各分析
工具独立仓库、独立发版，perfbox 负责从源下载安装它们、在本机浏览器里
提供统一的工作台（选工具、填参数、看进度和报告）、管理版本更新与回退。

内置工具名录共 7 个工具，按问题域分组：

- 启动
  - startup-latency「启动时延一致性分析」：批量分析启动时延测试表格，
    定位偏离均值的样本，输出报告与 Excel。
- 卡顿
  - jank-analyzer「100ms 卡顿主线程分析」：批量分析 100ms 级卡顿
    trace 的主线程耗时，输出 Markdown 报告与 CSV。
- IO
  - blkscope「块设备 IO 分析」：分析 Perfetto trace 中的块设备 IO，
    每个 trace 输出一份 HTML 报告。
- 内存
  - memscope「内存测试包 A/B 对比」：对比两个内存测试包，输出
    HTML、Markdown 和 Excel 报告。
  - vmscope「整机内存时序分析」：解析 mobilelog 里的 kernel log 与
    events_log，输出整机内存时序报告。
  - meminfo-diff「meminfo 双版本对比」：对比两个版本的 cat_meminfo
    采样，输出单文件 HTML 报告。
- Trace
  - perfetto-trim「Trace 裁剪与切分」：按时间裁剪、切分 perfetto
    trace，或按关键字查找。

## 下载

从 <https://github.com/thomascqdx/perfbox/releases/latest> 下载
`perfbox.exe`，单个文件，无需安装。建议放进一个专门的目录，例如
`D:\perfbox`（见「结果在哪里」）。

perfbox.exe 没有签名。首次运行时 Windows SmartScreen 可能提示「已阻止
启动无法识别的应用」：点「更多信息」→「仍要运行」。公司杀毒软件如果
拦截或删除文件，改用「离线使用」一节的共享盘源方式获取工具（拦截的是
工具下载流量时同样适用）。

## 首次运行

双击 `perfbox.exe`：会开一个控制台窗口，打印一行访问地址，形如
`http://127.0.0.1:端口/#t=…`，同时自动用默认浏览器打开这个地址——
这就是工作台。**控制台窗口必须保持开着，关掉它就退出 perfbox。**

工具不会随 perfbox 一起分发：第一次安装或使用某个工具时，perfbox 才
从 GitHub Release 下载它的产物（约几 MB 到几十 MB）。在「工具管理」页
可以看到每个工具的已装版本、最新版本，并进行安装、更新、回退、卸载。

## 结果在哪里

结果放在**工作目录**下：

- 双击启动 → 工作目录是 `perfbox.exe` 所在的目录。
- 命令行启动 → 工作目录是启动时的当前目录。

每个任务的结果在 `<工作目录>\out\<任务标识>\` 下；任务页面和状态栏都会
显示实际位置。建议把 `perfbox.exe` 放在专门目录（如 `D:\perfbox`），
避免结果散落在下载目录里。

任务记录、日志、工具库和配置放在 `%LOCALAPPDATA%\perfbox`（可用环境变
量 `PERFBOX_HOME` 改）。移动 `perfbox.exe` 到别的目录不影响历史记录。

## 配置共享盘源

源是 perfbox 获取工具清单和产物的位置。默认只有 GitHub Release 源；
团队里通常再挂一个共享盘目录源。打开工作台的「设置」页添加目录源，
或直接编辑 `%LOCALAPPDATA%\perfbox\config.json`：

```json
{
  "sources": [
    {"type": "dir", "path": "\\\\fileserver\\perf\\perfbox"},
    {"type": "github"}
  ],
  "proxy": null,
  "max_parallel_jobs": 1
}
```

- `sources` 按顺序生效：找清单、装工具、查更新时依次问每个源，用第一
  个有的。共享盘放在 GitHub 前面即「优先内网，GitHub 兜底」。
- `proxy` 是访问 GitHub 时的 HTTP 代理，不需要就留 `null`；也可由环
  境变量 `HTTPS_PROXY` 提供（见「代理」）。
- `max_parallel_jobs` 是任务并发上限，缺省 1。

## 离线使用

目标机器完全不能联网时，用 `perfbox export` 做一份目录源带过去：

1. 在一台能联网的机器上装好 perfbox 和需要的工具，然后运行：

   ```powershell
   perfbox export D:\perfbox-tools
   ```

   导出目录按「目录源」布局存放全部已装工具的产物和清单。
2. 把 `D:\perfbox-tools` 整个目录拷到离线机器（U 盘或共享盘均可）。
3. 在离线机器上把它配成目录源（「设置」页或 config.json），或者启动
   前设环境变量：

   ```powershell
   $env:PERFBOX_SOURCE = 'D:\perfbox-tools'
   .\perfbox.exe
   ```

   `PERFBOX_SOURCE` 优先于配置文件里的全部源。

## 命令行

`perfbox.exe` 同时是命令行工具：

```powershell
perfbox                         # 无参数等同 perfbox ui
perfbox ui [--port N] [--no-browser]   # 启动本机界面
perfbox list                    # 名录中的工具、已装版本、可用新版
perfbox install <tool>          # 安装工具
perfbox update [<tool> | --all] # 更新工具
perfbox rollback <tool>         # 回退到上一版
perfbox run <tool> -- <args>    # 参数原样传给工具，适合脚本调用
perfbox export <dir>            # 把已装工具导出成目录源布局
perfbox manifest check <file>   # 校验一份清单
```

`perfbox run` 让命令行用户和脚本也只需要记住一个入口，例如
`perfbox run memscope -- -V`。

## 代理

访问 GitHub 不通时配 HTTP 代理：在工作台「设置」页填写，或在
config.json 里写 `"proxy": "http://主机:端口"`；也可以设环境变量
`HTTPS_PROXY`（命令行运行时生效）。

## 常见问题

- **浏览器没打开 / 端口被占**：从控制台窗口复制打印的完整地址（含
  `#t=` 部分）粘到浏览器。`#t=` 是本次启动的访问凭据，不要发给别
  人。
- **杀毒软件拦截**：perfbox 本体或工具下载被杀软拦截时，让管理员把
  目录加入白名单，或改用共享盘目录源，GitHub 源只留给能联网的机
  器。
- **工作目录不可写**：把 `perfbox.exe` 移到可写目录再启动，或换到可
  写目录后用命令行启动。
- **工具更新与回退**：在「工具管理」页操作；每个工具可同时保留新旧
  两个版本，更新后可用「回退到 x.y.z」切回。
- **perfbox 自身有新版**：状态栏出现「perfbox x.y.z 可用」时，点
  「下载新版」到 release 页下载替换 exe 即可，已装工具和历史记录不
  受影响。

## 开发

仓库内的开发约定见 `AGENTS.md`；架构、清单规范和里程碑见 `docs/`。
