# perfbox 实现方案

状态：草案 v1，2026-11-13。术语见 `CONTEXT.md`，清单格式见 `tool-manifest.md`，关键决策见 `adr/`。

## 1. 目标与非目标

目标：

- **一个入口**：双击 `perfbox.exe`，在浏览器里选工具、填参数、看进度、开报告。
- **工具保持独立**：各工具独立仓库、独立发版，脱离 perfbox 仍可单独使用。
- **零配置可用**：未配置源时，从各工具的 GitHub Release 取最新版。
- **可配置源**：配置后改走共享盘、内网目录或离线目录。
- **首包小**：宿主不含任何工具，工具按需下载，可更新、可回退。

v1 不做：

- 不合并、不重写各工具的分析内核和报告页面。
- 不做体积去重，每个产物仍自带 Python 运行时。
- 只支持 Windows x64。
- 只监听 127.0.0.1，不做多用户或远程服务。
- 宿主不自我替换升级，只提示有新版。

## 2. 现状基线（2026-11-13 核实）

| 工具 | 问题域 | GitHub | 最新 release | 产物形态 | 版本号来源 |
|---|---|---|---|---|---|
| memscope | 内存 | public | v0.8.1，`memscope-0.8.1.exe` | 单 exe，10 MB | `src/memscope/__init__.py` |
| perfetto-trim | Trace | public | v1.3.0，`perfetto-trim.exe` | 单 exe，12 MB | `src/pftrim/__init__.py` |
| blkscope | IO | public | 无 release、无 tag | zip（内含单 exe），35 MB | `src/blkscope/__init__.py` |
| vmscope | 内存 | 无 remote | 无 | 单 exe，10 MB | `src/vmscope/__init__.py` |
| meminfo_diff | 内存 | 无 remote | 无 | 单 exe，12 MB | `src/meminfo_diff/__init__.py` |
| jank-100ms-analysis | 卡顿 | origin 指向本地目录 | 无（本地 tag v2.1.0） | 便携 zip，16 MB | `jank_cli.py` |
| dynamic_model_tools | 启动 | 无 remote | 无（本地 tag v1.5.0） | 目录型 zip，35 MB | `latency_batch/__init__.py` |

各工具命令行的输出位置选项：

| 工具 | 选项 | 含义 | 结果文件 |
|---|---|---|---|
| memscope | `-o` | 目录 | `report.html`、`report.md`、`data.xlsx` |
| blkscope | `-o` | 目录 | 每个输入一个 `<name>.html` |
| vmscope | `-o` | 目录，每个输入一个子目录 | `report.html`、`summary.md`、`data.xlsx` |
| meminfo_diff | `-o` | 文件或目录 | `meminfo_diff_<base>_vs_<target>.html` |
| jank | `--output` | 必须是 `.md` 文件路径 | `.md` 和同名 `.csv` |
| dynamic_model_tools | `--output` | 目录，必须事先不存在 | 报告、Excel、`images/`、`analysis.json` |
| perfetto-trim | `-o` | 目录 | 裁剪后的 trace；另有自带网页界面 `ui --port N --no-browser` |

## 3. 架构

```
perfbox.exe（宿主：本机 HTTP 服务 + 前端静态资源，不含分析代码）
│
├─ 工作台（浏览器，127.0.0.1）
│    按问题域选工具 → 按清单生成参数表单 → 任务日志 → 打开报告
│
├─ server     HTTP 接口与静态文件
│    ├─ catalog    工具名录：有哪些工具、默认去哪里找
│    ├─ sources    从源取清单和产物（GitHub Release / 目录）
│    ├─ store      工具库：下载、校验、多版本、回退
│    ├─ manifest   解析清单，把参数值拼成命令行
│    └─ jobs       运行任务和应用，日志、取消、结果
│
└─ 工具（各自独立仓库、独立发版）
     产物（exe 或 zip）+ perfbox-tool.json
```

### 3.1 模块

每个模块给出接口（调用方需要知道的全部内容）和它藏在接口后面的东西。

**manifest**：纯函数，不做任何 I/O。

```python
def parse(text: str) -> Manifest            # 校验失败抛 ManifestError，信息指出字段路径
def build_argv(command: Command, values: dict, out_dir: Path, port: int | None) -> list[str]
```

藏起来的：schema 校验、位置参数与选项的排序、`multiple`/`count`、「未改动的参数不传」、`output` 的 `dir`/`file`/`fresh` 差异。它不知道安装目录，返回的命令行不含 `entry`。

**sources**：唯一有两个适配器的接缝。

```python
class Source(Protocol):
    def latest_manifest(self, tool: CatalogEntry) -> bytes | None       # None 表示这个源没有该工具
    def fetch_artifact(self, tool: CatalogEntry, manifest: Manifest,
                       dest: Path, progress: Callable[[int, int], None]) -> None

def configured(config: Config) -> list[Source]
```

- `GitHubSource(base_url="https://github.com")`：`base_url` 可改成同路径布局的镜像。
- `DirSource(root)`：本地或 UNC 目录。

藏起来的：URL 拼接、重定向、代理、超时与重试、目录源的版本排序。

源的选择规则：配置了源就只用配置的源，按顺序取第一个有该工具的；没配置时等同于只配了一个 GitHub 源。想同时保留 GitHub 兜底，就把它显式列在后面。

**store**：本机磁盘上的工具库。

```python
def installed(tool_id: str) -> InstalledTool | None      # 当前版本；含 entry 绝对路径和 Manifest
def install(tool: CatalogEntry, sources: list[Source], progress) -> InstalledTool
def check_update(tool: CatalogEntry, sources: list[Source]) -> str | None   # 有新版时返回版本号
def rollback(tool_id: str) -> InstalledTool
def remove(tool_id: str) -> None
```

藏起来的：`.part` 临时文件、大小与 sha256 校验、zip 解压（含路径穿越检查、去掉单一顶层目录）、原子改名、当前版本指针、保留上一版、正在运行的版本不删除、`schema` 和 `min_perfbox` 的兼容闸门。

**jobs**：进程的生命周期。

```python
def start_job(tool: InstalledTool, command_id: str, values: dict, out_dir: Path | None) -> Job
def start_app(tool: InstalledTool, command_id: str) -> App        # 已在运行则返回现有实例
def get(job_id: str) -> Job
def history() -> list[Job]                                        # 从磁盘恢复，重启后仍在
def cancel(job_id: str) -> None
def read_log(job_id: str, offset: int) -> tuple[bytes, int]
def shutdown() -> None                                            # 结束全部子进程
```

藏起来的：排队与并发上限、Windows Job Object（按进程树结束）、日志落盘与解码、按 `outputs` 收集结果、`job.json` 持久化、应用的端口分配与就绪检测。

**catalog**：内置 `catalog.json`，每个工具一条：`id`、`title`、`summary`、`domain`、`github`（`owner/repo`）。只用来显示尚未安装的工具和定位它的源。

**server**：照 `perfetto-trim/src/pftrim/web/server.py` 的做法写：标准库 `ThreadingHTTPServer`，随机端口，每次启动随机 token 经 URL fragment 交给页面，`/api/*` 校验 token 请求头和 Host。

**cli**：见 3.4。

### 3.2 本机数据布局

根目录缺省为 `%LOCALAPPDATA%\perfbox`，环境变量 `PERFBOX_HOME` 可覆盖（测试和便携模式用）。

```
perfbox\
├─ config.json                  源、代理、并发上限
├─ update-cache.json            最近一次更新检查的结果和时间
├─ downloads\*.part
├─ tools\<id>\
│    ├─ current.json            {"version": "0.8.1", "previous": "0.8.0"}
│    └─ <version>\              perfbox-tool.json + 产物（zip 已解压）
└─ jobs\<job-id>\
     ├─ job.json                工具、版本、命令行、参数、起止时间、退出码、结果列表
     ├─ log.txt
     └─ out\                    结果目录（用户可在提交任务时改到别处）
```

任务标识形如 `20261113-153012-memscope-a3f1`，按时间可排序，人也能读。

`config.json`：

```json
{
  "sources": [
    {"type": "dir", "path": "\\\\fileserver\\perf\\perfbox"},
    {"type": "github"}
  ],
  "proxy": "http://127.0.0.1:7890",
  "max_parallel_jobs": 1
}
```

`sources` 缺省为 `[{"type": "github"}]`。`proxy` 缺省取环境变量 `HTTPS_PROXY`。环境变量 `PERFBOX_SOURCE`（一个目录路径）优先于配置文件。

### 3.3 关键流程

**安装或更新一个工具**

1. 按顺序问各个源要最新清单，取第一个有的。
2. 解析清单。`schema` 不认识或 `min_perfbox` 高于当前宿主版本时，停止并保留已装版本，提示需要升级 perfbox。
3. 下载产物到 `downloads\*.part`，校验大小和 sha256。
4. exe 直接放入 `tools\<id>\<version>.tmp\`，zip 解压到同一位置；然后改名为 `<version>\`。
5. 更新 `current.json`，原当前版本记为 `previous`。更早的版本在没有任务使用时删除。

**更新检查**

- 启动后在后台线程进行，不阻塞界面；失败时静默使用本地状态。
- 结果写入 `update-cache.json`，6 小时内不重复检查。
- 只提示，不自动更新。分析做到一半时工具版本悄悄变了，前后两份报告会不可比。

**运行一个任务**

1. 取工具的当前版本；未安装则先安装。
2. `manifest.build_argv` 拼出参数，前面接上 `entry`。
3. 建任务目录，写 `job.json`，进入队列。并发上限缺省为 1（memscope 这类工具单次要占几 GB 临时空间）。
4. 启动子进程：工作目录为任务目录，stdin 关闭，stdout 与 stderr 合并写入 `log.txt`，进程放进一个 Job Object。
5. 进程退出后按清单的 `outputs` 收集结果，更新 `job.json`。

**取消**：结束整个 Job Object。PyInstaller 单文件 exe 是「引导进程 + 子进程」两层，只结束第一层会留下孤儿进程。

**运行一个应用**：挑一个空闲端口，启动进程，逐行读 stdout 直到匹配 `ready.url_regex`，把地址交给前端在新标签页打开。每个工具同时只有一个应用实例。宿主退出时结束全部应用。

### 3.4 命令行

```
perfbox                         无参数等同于 perfbox ui
perfbox ui [--port N] [--no-browser]
perfbox list                    名录中的工具、已装版本、可用新版
perfbox install <tool>
perfbox update [<tool> | --all]
perfbox rollback <tool>
perfbox run <tool> -- <原样传给工具的参数>
perfbox export <dir>            把已装工具导出成目录源的布局
perfbox manifest check <file>   校验一份清单
```

`perfbox run` 不做参数翻译，只负责找到（必要时安装）工具并把参数原样传过去，继承当前终端。它让命令行用户和脚本也只需要记住一个入口。

### 3.5 HTTP 接口

| 方法与路径 | 作用 |
|---|---|
| `GET /api/state` | 宿主版本、源、全部工具（名录合并已装状态与可用更新） |
| `GET /api/tools/<id>` | 该工具当前版本的清单（未安装时取源上的最新清单） |
| `POST /api/tools/<id>/install`、`/update`、`/rollback`、`/remove` | 工具管理；下载是后台操作，进度从 `/api/state` 读 |
| `POST /api/jobs` | 提交任务：`{tool, command, values, out_dir?}` |
| `GET /api/jobs` | 任务历史 |
| `GET /api/jobs/<id>?log_offset=N` | 任务状态、结果列表、从偏移量起的新日志 |
| `POST /api/jobs/<id>/cancel` | 取消 |
| `POST /api/apps` | 启动应用：`{tool, command}`，返回访问地址 |
| `POST /api/pick` | 原生文件或目录选择框：`{kind: "file" \| "dir", extensions?}`，返回路径 |
| `POST /api/reveal` | 在资源管理器中显示某个结果文件 |
| `GET /api/config`、`POST /api/config` | 读写源和代理 |
| `GET /files/<token>/<job-id>/<相对路径>` | 读取结果文件，供新标签页或内嵌预览 |

- 浏览器拿不到本机真实路径，所以路径参数用「文本框 + 浏览按钮」：可以粘贴（含 UNC 路径），也可以调原生对话框。
- `/files/` 把 token 放在路径里而不是查询串里，这样报告里的相对链接（如 `images/x.png`）仍然有效。只允许访问任务结果目录之内的文件。

## 4. 里程碑

每个里程碑结束时：测试全绿，改 `__version__`，提交，打 tag。

### M0 仓库与约定（本次完成）

- `git init`，分支 `main`，`.gitignore`、`.gitattributes`。
- `AGENTS.md`、`CONTEXT.md`、本文件、`tool-manifest.md`、两份 ADR。

### M1 清单契约与命令行闭环 → v0.1.0

目标：不带界面，从命令行装上并跑起 memscope 和 perfetto-trim。

宿主：

1. 工程骨架：`pyproject.toml`（`src/` 布局，入口 `perfbox = perfbox.cli:main`）、`src/perfbox/__init__.py`、`paths.py`（数据根目录与 `config.json` 读写）、pytest。
2. `manifest`：解析、校验、`build_argv`。先写测试，用例直接取自 `tool-manifest.md` 第 9 节。
3. `sources`：先 `DirSource`，后 `GitHubSource`。
4. `store`：安装、回退、删除、兼容闸门。
5. `catalog.json`：先收 memscope、perfetto-trim 两条。
6. `cli`：`list`、`install`、`update`、`rollback`、`run`、`manifest check`。
7. `tools/stamp_manifest.py`：供工具仓库拷贝。

工具侧：

8. memscope、perfetto-trim 各加 `perfbox-tool.json`、`scripts/stamp_manifest.py` 和契约测试。
9. 为两者现有的最新 release 补传清单。sha256 按已发布的产物计算，不需要重新构建，也不需要发新版本。**这一步会改动 GitHub 上的 release，执行前需要确认。**

本里程碑要回答的三个验证点（见第 7 节）：管道下各工具的输出编码；下载得到的 exe 在同事机器上是否被杀毒软件拦截；`releases/latest/download/` 在公司网络下是否稳定。

验收：

- 在全新的 `PERFBOX_HOME` 下，`perfbox run memscope -- -V` 自动下载、校验并打印版本号。
- `perfbox run perfetto-trim -- --version` 同上。
- 断网时再次运行，直接使用已装版本，没有报错和明显等待。
- 把 `PERFBOX_SOURCE` 指向一个本地目录源，行为一致。
- 篡改目录源中的产物后，安装失败并指出校验不符，已装版本不受影响。

### M2 宿主服务与工作台 → v0.2.0

目标：在浏览器里完成「选 memscope → 填两个包 → 看日志 → 打开报告」。

1. `jobs`：任务部分——排队、Job Object、日志、取消、结果收集、历史持久化。
2. `server`：token 与 Host 校验、静态文件、`/api/state`、`/api/tools/<id>`、`/api/jobs*`、`/api/pick`、`/api/reveal`、`/files/`。
3. 前端（开始前加载 `frontend-design` 技能）：
   - 左侧按问题域列出工具，标明未安装、已安装、有更新。
   - 工具页：命令切换、按清单生成的表单、高级选项折叠、提交。
   - 任务页：实时日志、取消、结果列表、打开主报告、在资源管理器中显示。
   - 历史页：过往任务，可重新打开报告，可用相同参数再次提交。
4. `cli`：`ui` 子命令，无参数时进入界面。

验收：

- 用 `memscope/samples` 下的静态包跑通 `static-compare`，主报告在新标签页打开。
- 运行中取消后，任务管理器里没有残留的 `memscope` 进程。
- 关闭并重启 perfbox，历史任务仍在，报告仍可打开。
- 不带 token 访问 `/api/state` 返回 403；`/files/` 无法读到结果目录之外的文件。
- 输入路径含中文和空格时结果正确。

### M3 应用型命令与工具管理界面 → v0.3.0

1. `jobs`：应用部分——端口分配、就绪检测、单实例、宿主退出时清理。
2. 接口：`/api/apps`、`/api/tools/<id>/install|update|rollback|remove`、`/api/config`。
3. 前端：
   - 应用型命令显示「打开」按钮，在新标签页进入工具自带界面。
   - 工具管理：安装进度、一键全部更新、回退到上一版、卸载。
   - 设置页：源列表（目录或 GitHub）、代理。
4. 后台更新检查与 `update-cache.json`。

验收：

- 点击 perfetto-trim 的「打开」，其界面可用；再点一次不会启动第二个进程。
- 退出 perfbox 后没有残留的 `perfetto-trim` 进程。
- 断网启动，界面在一秒内可用，工具状态显示为本地已装版本。
- 目录源里放一个更高版本，界面提示有更新；更新后再回退，两个方向都正确。
- 清单 `schema` 为 2 的版本被拒绝，界面提示需要升级 perfbox。

### M4 接入其余五个工具 → v0.4.0

按下面的顺序逐个接入，每接入一个就提交一次。每个工具的具体改动见第 5 节。

1. **blkscope**：仓库已在 GitHub，只缺 release。
2. **vmscope**、**meminfo_diff**：单 exe，命令行已是非交互。
3. **jank-100ms-analysis**：第一个 zip 产物，验证 `entry` 列表和去顶层目录。
4. **dynamic_model_tools**：目录型 zip，验证 `output.fresh`。

宿主侧只有两件事：往 `catalog.json` 加条目；修掉接入过程中暴露的清单表达力不足（改规范要同步改 `tool-manifest.md`）。

验收：七个工具都能从界面安装并各跑通一个真实样本；每个工具仓库的契约测试通过。

### M5 打包与分发 → v0.5.0

1. `scripts/perfbox_entry.py`、`scripts/build_exe.ps1`、`build.bat`：PyInstaller 单文件，静态资源打入，结束时做 `--version` 自检。做法照 perfetto-trim 的 `build.bat`。
2. `perfbox export <dir>`：把已装工具写成目录源布局，用于共享盘和离线分发。
3. 宿主新版提示：查 `releases/latest` 的重定向目标，与 `__version__` 比较，界面给出下载链接。
4. `README.md`：面向同事的使用说明——下载、首次运行、配置共享盘源、离线使用。
5. 在一台没有 Python、没有代理的同事机器上完整验收。

验收：

- `perfbox.exe` 在 PATH 中没有 Python 的机器上运行正常。
- 导出的目录拷到另一台断网机器，设为源后七个工具都能安装。
- 同事按 README 独立完成一次分析，中途不需要问人。

试用稳定后发 v1.0.0。

### M6 之后（按需）

- **结构化进度**：约定 stdout 上的进度行，界面显示阶段和百分比。
- **跨工具流转**：例如把 perfetto-trim 裁出的 trace 直接送进 blkscope。
- **静态页面型命令**：dynamic_model_tools 的 `web/` 下两个纯浏览器工具。
- **一个仓库多个产物**：blkscope 的 `startup_firstframe_io.exe`。
- **GitHub Actions 构建发版**：目前各工具都在本机构建。

## 5. 各工具仓库的改动清单

所有工具共同的四项：

1. 根目录加 `perfbox-tool.json`（版本和产物校验字段留空）。
2. 加 `scripts/stamp_manifest.py`，构建脚本在产物生成后调用它。
3. 加契约测试 `tests/test_perfbox_manifest.py`：清单里的每个 `flag` 和子命令都存在于 argparse 定义中。为此需要把构造 parser 的代码放进一个可单独调用的函数（meminfo_diff 已有 `build_parser()`）。
4. 发版时把产物和 `dist/perfbox-tool.json` 一起上传。把这一步写进该仓库的 `AGENTS.md`。

各工具额外的事项：

| 工具 | 额外事项 |
|---|---|
| memscope | 无。两个命令 `static-compare`、`dynamic-compare` 共用一套参数；阈值类选项全部标为高级 |
| perfetto-trim | 无。先只暴露应用型命令 `ui`；`cut`、`split`、`find` 依赖交互选点，留在它自己的界面里 |
| blkscope | 发第一个 release（先打 tag）。产物用现有的 `blkscope-<ver>-windows-x64.zip`，`entry` 为 `["blkscope.exe"]`。输入是可多选的位置参数 |
| vmscope | 建 GitHub 仓库并推送；发 release。输入是可多选的位置参数；结果在 `-o` 下按输入名分子目录，`outputs` 用 `**/report.html` |
| meminfo_diff | 建 GitHub 仓库并推送；发 release。`-o` 传目录 |
| jank-100ms-analysis | 建 GitHub 仓库（当前 origin 是本地目录，新增一个 remote）；发 release。`entry` 取自 `run.cmd`；`output` 为 `file` 型，文件名 `jank_analysis.md` |
| dynamic_model_tools | 建 GitHub 仓库并推送；发 release。`output.fresh` 为 `true`。推送前检查 `data/`、`reports/`、`verification/`、`output/` 里有没有不该公开的内部数据 |

推送到 public 仓库的前提是内容可以公开。不能公开的工具不进 GitHub，只通过目录源分发，`catalog.json` 里该条目不写 `github` 字段。

## 6. 测试策略

- pytest，测试不联网、不依赖任何真实工具。
- **manifest**：纯函数，表驱动测试。覆盖每种参数类型、位置参数排序、`multiple`/`count`、未改动参数不传、`output` 三种形态、各类校验错误。
- **sources**：`DirSource` 用临时目录。`GitHubSource` 对一个本机 `ThreadingHTTPServer` 测试（把 `base_url` 指过去），覆盖重定向、404、中途断开、超时。
- **store**：用 `DirSource` 加临时 `PERFBOX_HOME`。覆盖校验失败不留残余、zip 路径穿越被拒、去顶层目录、回退、兼容闸门。只检查磁盘状态，不执行产物。
- **jobs**：直接构造 `InstalledTool`，`entry` 设为 `[sys.executable, 假工具脚本]`。假工具脚本按参数决定行为：写结果文件、持续输出日志、睡眠（测取消）、再启动一个子进程（测进程树清理）、以非 0 退出、输出非 UTF-8 字节。
- **server**：照 `perfetto-trim/tests/unit/test_web.py`。覆盖 token 与 Host 拒绝、路径穿越、从提交任务到读取结果的全流程。
- **联网冒烟**：标记为 `net`，默认跳过。对真实的 memscope 和 perfetto-trim release 跑一次安装。
- **界面**：每个里程碑结束时用全局 Python 的 playwright 截图自查，它不是项目依赖。

## 7. 风险与验证点

| 风险 | 何时验证 | 应对 |
|---|---|---|
| 冻结后的工具在管道下不按 UTF-8 输出，日志出现乱码 | M1 逐个工具实测 | 宿主解码时 UTF-8 失败则回退到系统代码页；同时在工具侧补 `reconfigure(encoding='utf-8')`（meminfo_diff 已有） |
| 程序下载的未签名 exe 被公司杀毒软件拦截 | M1，找一台同事机器 | 不能解决则同事一律走共享盘目录源，GitHub 源只留给自己用 |
| 公司网络访问 GitHub 不稳定 | M1 | 支持代理；失败快速返回并回落到已装版本；主推目录源 |
| 取消任务后留下孤儿进程 | M2 | Job Object；不可用时退回 `taskkill /T /F` |
| 宿主被强制结束，应用进程残留 | M3 | Job Object 设为句柄关闭即结束全部进程 |
| 清单与工具真实命令行不一致 | 每个工具接入时 | 工具仓库内的契约测试 |
| 工具发了不兼容的新清单，旧宿主拉到后出错 | M3 | `schema` 与 `min_perfbox` 闸门；保留上一版可回退 |
| 大包分析占满系统盘临时空间 | M2 | 并发上限缺省为 1；清单里暴露 `--tmpdir` |
| 超长路径或 UNC 路径 | M4 | 宿主原样传递路径；任务目录本身保持短路径 |

## 8. git 管理

**本仓库**

- 分支：只有 `main`，小步直接提交。一个提交只做一件事，测试与实现同一个提交。
- 提交信息：英文一句话标题，需要时正文说明原因，结尾附 Devin 署名行。
- 版本：唯一来源 `src/perfbox/__init__.py` 的 `__version__`。1.0 之前，里程碑升次版本号，修复升修订号。
- tag：附注 tag `vX.Y.Z`，打在改版本号的提交上。
- 不进仓库的内容：虚拟环境、构建产物、任务输出、下载的工具产物、样本数据。
- 远端：`thomascqdx/perfbox`，需要是 public，否则同事的宿主查不到新版。创建和推送都等用户确认。

**工具仓库**

- 每个工具的接入改动在它自己的仓库里提交，遵守它自己的 `AGENTS.md`（memscope 要求用户可见的改动同提交升版本号，perfetto-trim 发版提交用 `Release X.Y.Z: ...`）。
- 增加清单属于打包层面的改动。memscope 按其规则升修订号；其余仓库随下一次发版带出。
- 清单规范的改动先在本仓库提交，再改各工具仓库。

## 9. 待确认

1. **perfbox 的 GitHub 仓库**：是否现在创建 `thomascqdx/perfbox`（public）并推送。
2. **四个未上 GitHub 的工具**：vmscope、meminfo_diff、jank-100ms-analysis、dynamic_model_tools 是否都可以公开。不能公开的只走目录源。
3. **结果的默认位置**：目前定为 `%LOCALAPPDATA%\perfbox\jobs\<job-id>\out`，提交任务时可改。另一种做法是默认放到 `文档\perfbox\` 下，更容易找到，但会和任务日志分开存放。
4. **为已有 release 补传清单**：M1 第 9 步需要改动 memscope v0.8.1 和 perfetto-trim v1.3.0 的 release 附件。
