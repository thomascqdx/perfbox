# perfbox 实现方案

状态：v2，2026-11-13，已确认，可执行。术语见 `CONTEXT.md`，清单格式见 `tool-manifest.md`，关键决策见 `adr/`。

本文件同时是进度记录：第 4 节的勾选框表示完成情况，执行方式见第 10 节。

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

### 2.1 工具标识与仓库

工具标识用于清单的 `id`、工具库目录名和 `perfbox run <id>`。GitHub 仓库名沿用本地目录名。

| 工具标识 | 界面名称 | 问题域 | 本地目录 | GitHub 仓库 |
|---|---|---|---|---|
| `memscope` | 内存测试包 A/B 对比 | 内存 | `memscope` | `thomascqdx/memscope`（已有，public） |
| `vmscope` | 整机内存时序分析 | 内存 | `vmscope` | `thomascqdx/vmscope`（待建） |
| `meminfo-diff` | meminfo 双版本对比 | 内存 | `meminfo_diff` | `thomascqdx/meminfo_diff`（待建） |
| `blkscope` | 块设备 IO 分析 | IO | `blkscope` | `thomascqdx/blkscope`（已有，public） |
| `jank-analyzer` | 100ms 卡顿主线程分析 | 卡顿 | `jank-100ms-analysis` | `thomascqdx/jank-100ms-analysis`（待建） |
| `startup-latency` | 启动时延一致性分析 | 启动 | `dynamic_model_tools` | `thomascqdx/dynamic_model_tools`（待建） |
| `perfetto-trim` | Trace 裁剪与切分 | Trace | `perfetto-trim` | `thomascqdx/perfetto-trim`（已有，public） |

### 2.2 发版状态

| 工具 | 当前版本 | tag 与 HEAD | GitHub release | 产物形态 | 版本号来源 |
|---|---|---|---|---|---|
| memscope | 0.8.1 | HEAD 在 `v0.8.1` 上 | v0.8.1，`memscope-0.8.1.exe` | 单 exe，10 MB | `src/memscope/__init__.py` |
| perfetto-trim | 1.3.0 | HEAD 在 `v1.3.0` 之后 2 个提交（仅文档） | v1.3.0，`perfetto-trim.exe` | 单 exe，12 MB | `src/pftrim/__init__.py` |
| blkscope | 0.6.0 | 无 tag | 无 | zip，内含 `blkscope.exe`，35 MB | `src/blkscope/__init__.py` |
| vmscope | 0.4.0 | 无 tag | 无 | 单 exe，10 MB | `src/vmscope/__init__.py` |
| meminfo_diff | 0.1.1 | 无 tag | 无 | 单 exe，12 MB | `src/meminfo_diff/__init__.py` |
| jank-100ms-analysis | 2.1.0 | HEAD 在 `v2.1.0` 上 | 无 | 便携 zip，16 MB | `jank_report.py` 的 `VERSION` |
| dynamic_model_tools | 1.5.0 | HEAD 在 `v1.5.0` 之后 2 个提交 | 无 | 目录型 zip，35 MB | `latency_batch/__init__.py` |

工作区状态：除 blkscope 有一个未跟踪文件 `scripts/whitelist.txt.txt` 外，其余全部干净。

### 2.3 各工具的输出位置选项

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

藏起来的：结果目录的确定、排队与并发上限、Windows Job Object（按进程树结束）、日志落盘与解码、按 `outputs` 收集结果、`job.json` 持久化、应用的端口分配与就绪检测。

**catalog**：内置 `catalog.json`，每个工具一条：`id`、`title`、`summary`、`domain`、`github`（`owner/repo`）。只用来显示尚未安装的工具和定位它的源。内容取自 2.1 节。

**server**：照 `perfetto-trim/src/pftrim/web/server.py` 的做法写：标准库 `ThreadingHTTPServer`，随机端口，每次启动随机 token 经 URL fragment 交给页面，`/api/*` 校验 token 请求头和 Host。

**cli**：见 3.5。

### 3.2 结果放在哪里

结果放在工作目录下。工作目录是 perfbox 进程启动时的当前目录：

- 从命令行启动时，是执行 `perfbox` 时所在的目录。
- 双击 `perfbox.exe` 时，是 exe 所在的目录。
- 通过快捷方式启动时，是快捷方式的「起始位置」。

规则：

- 每个任务的结果目录是 `<工作目录>\out\<任务标识>\`。
- 工具进程的当前目录也是工作目录，所以表单里填的相对路径按工作目录解析。
- 提交任务时可以把结果目录改到别处，只对这一个任务生效。
- 工作目录不可写时，提交任务直接报错并说明原因，不悄悄换地方。
- `perfbox run <tool> -- ...` 不指定输出位置，由工具自己的默认规则决定。各工具的默认规则本来就是当前目录下的 `out\` 或当前目录。

任务标识形如 `20261113-153012-memscope-a3f1`，按时间可排序，人也能读。

### 3.3 本机数据布局

宿主自己的数据放在数据根目录，缺省为 `%LOCALAPPDATA%\perfbox`，环境变量 `PERFBOX_HOME` 可覆盖（测试和便携模式用）。

```
%LOCALAPPDATA%\perfbox\
├─ config.json                  源、代理、并发上限
├─ update-cache.json            最近一次更新检查的结果和时间
├─ downloads\*.part
├─ tools\<id>\
│    ├─ current.json            {"version": "0.8.1", "previous": "0.8.0"}
│    └─ <version>\              perfbox-tool.json + 产物（zip 已解压）
└─ jobs\<任务标识>\
     ├─ job.json                工具、版本、命令行、参数、起止时间、退出码、结果目录的绝对路径、结果列表
     └─ log.txt

<工作目录>\
└─ out\<任务标识>\              结果
```

任务记录和日志留在数据根目录，结果目录里只有工具写出的文件。这样无论从哪个目录启动 perfbox，历史页都能列出全部任务。结果目录被移走或删除后，历史页把该任务标为「结果已不在原位置」。

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

### 3.4 关键流程

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
2. 确定结果目录（3.2 节）。`output.fresh` 为 `true` 时只建到 `out\` 这一层，任务自己的目录留给工具创建。
3. `manifest.build_argv` 拼出参数，前面接上 `entry`。
4. 在数据根目录建任务记录，写 `job.json`，进入队列。并发上限缺省为 1（memscope 这类工具单次要占几 GB 临时空间）。
5. 启动子进程：当前目录为工作目录，stdin 关闭，stdout 与 stderr 合并写入 `log.txt`，进程放进一个 Job Object。
6. 进程退出后按清单的 `outputs` 在结果目录里收集结果，更新 `job.json`。

**取消**：结束整个 Job Object。PyInstaller 单文件 exe 是「引导进程 + 子进程」两层，只结束第一层会留下孤儿进程。

**运行一个应用**：挑一个空闲端口，启动进程，逐行读 stdout 直到匹配 `ready.url_regex`，把地址交给前端在新标签页打开。每个工具同时只有一个应用实例，不占任务的并发名额。宿主退出时结束全部应用。

### 3.5 命令行

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

`perfbox run` 不做参数翻译，只负责找到（必要时安装）工具并把参数原样传过去，继承当前终端和当前目录。它让命令行用户和脚本也只需要记住一个入口。

### 3.6 HTTP 接口

| 方法与路径 | 作用 |
|---|---|
| `GET /api/state` | 宿主版本、工作目录、源、全部工具（名录合并已装状态与可用更新） |
| `GET /api/tools/<id>` | 该工具当前版本的清单（未安装时取源上的最新清单） |
| `POST /api/tools/<id>/install`、`/update`、`/rollback`、`/remove` | 工具管理；下载是后台操作，进度从 `/api/state` 读 |
| `POST /api/jobs` | 提交任务：`{tool, command, values, out_dir?}` |
| `GET /api/jobs` | 任务历史 |
| `GET /api/jobs/<id>?log_offset=N` | 任务状态、结果目录、结果列表、从偏移量起的新日志 |
| `POST /api/jobs/<id>/cancel` | 取消 |
| `POST /api/apps` | 启动应用：`{tool, command}`，返回访问地址 |
| `POST /api/pick` | 原生文件或目录选择框：`{kind: "file" \| "dir", extensions?}`，返回路径 |
| `POST /api/reveal` | 在资源管理器中显示某个结果文件 |
| `GET /api/config`、`POST /api/config` | 读写源和代理 |
| `GET /files/<token>/<job-id>/<相对路径>` | 读取结果文件，供新标签页或内嵌预览 |

- 浏览器拿不到本机真实路径，所以路径参数用「文本框 + 浏览按钮」：可以粘贴（含 UNC 路径），也可以调原生对话框。
- `/files/` 把 token 放在路径里而不是查询串里，这样报告里的相对链接（如 `images/x.png`）仍然有效。
- `/files/` 只允许读取该任务 `job.json` 里记录的结果目录之内的文件。

## 4. 里程碑

每个里程碑结束时：测试全绿，改 `__version__`，提交，打 tag，推送。

### M0 仓库与约定

- [x] `git init`，分支 `main`，`.gitignore`、`.gitattributes`。
- [x] `AGENTS.md`、`CONTEXT.md`、本文件、`tool-manifest.md`、两份 ADR。
- [x] 创建 `thomascqdx/perfbox`（public）并推送。

### M1 清单契约与命令行闭环 → v0.1.0

目标：不带界面，从命令行装上并跑起 memscope 和 perfetto-trim。

宿主：

- [x] 1. 工程骨架：`pyproject.toml`（`src/` 布局，入口 `perfbox = perfbox.cli:main`）、`src/perfbox/__init__.py`、`paths.py`（数据根目录与 `config.json` 读写）、pytest、`.venv`。把搭建、测试、运行命令补进 `AGENTS.md`。
- [x] 2. `manifest`：解析、校验、`build_argv`。先写测试，用例直接取自 `tool-manifest.md` 第 9 节。
- [x] 3. `sources`：先 `DirSource`，后 `GitHubSource`。
- [x] 4. `store`：安装、回退、删除、兼容闸门。
- [x] 5. `catalog.json`：先收 memscope、perfetto-trim 两条。
- [x] 6. `cli`：`list`、`install`、`update`、`rollback`、`run`、`manifest check`。
- [x] 7. `tools/stamp_manifest.py`：供工具仓库拷贝。

工具侧：

- [x] 8. memscope：加 `perfbox-tool.json`、`scripts/stamp_manifest.py`、契约测试；构建脚本在产物生成后调用 stamp。
- [x] 9. perfetto-trim：同上。
- [x] 10. 为 memscope v0.8.1 和 perfetto-trim v1.3.0 的现有 release 补传清单，然后推送两个仓库的接入提交。做法见 5.2 节。

验证点（见第 7 节）：

- [x] 11. 实测 memscope 和 perfetto-trim 在管道下的输出编码，结论写进第 7 节。
- [x] 12. 准备「同事机器杀毒软件」和「公司网络访问 GitHub」两项验证的操作步骤，记入第 11 节，不等结果。

验收：

- [x] 在全新的 `PERFBOX_HOME` 下，`perfbox run memscope -- -V` 自动下载、校验并打印版本号。
- [x] `perfbox run perfetto-trim -- --version` 同上。
- [x] 断网时再次运行，直接使用已装版本，没有报错和明显等待。
- [x] 把 `PERFBOX_SOURCE` 指向一个本地目录源，行为一致。
- [x] 篡改目录源中的产物后，安装失败并指出校验不符，已装版本不受影响。

### M2 宿主服务与工作台 → v0.2.0

目标：在浏览器里完成「选 memscope → 填两个包 → 看日志 → 打开报告」。

- [x] 1. `jobs`：任务部分——结果目录、排队、Job Object、日志、取消、结果收集、历史持久化。
- [x] 2. `server`：token 与 Host 校验、静态文件、`/api/state`、`/api/tools/<id>`、`/api/jobs*`、`/api/pick`、`/api/reveal`、`/files/`。
- [x] 3. 前端（开始前加载 `frontend-design` 技能）：
   - 左侧按问题域列出工具，标明未安装、已安装、有更新。
   - 工具页：命令切换、按清单生成的表单、高级选项折叠、结果目录（显示默认值，可改）、提交。
   - 任务页：实时日志、取消、结果列表、打开主报告、在资源管理器中显示。
   - 历史页：过往任务，可重新打开报告，可用相同参数再次提交。
- [x] 4. `cli`：`ui` 子命令，无参数时进入界面。

验收：

- [ ] 用 `memscope/samples` 下的静态包跑通 `static-compare`，主报告在新标签页打开。
- [ ] 从 `D:\work` 这样的目录启动，结果出现在 `D:\work\out\<任务标识>\` 下，该目录里没有 perfbox 自己的文件。
- [ ] 从一个不可写的目录启动，提交任务时得到明确的错误提示。
- [ ] 运行中取消后，任务管理器里没有残留的 `memscope` 进程。
- [ ] 换一个目录重启 perfbox，历史任务仍在，报告仍可打开。
- [ ] 不带 token 访问 `/api/state` 返回 403；`/files/` 无法读到结果目录之外的文件。
- [ ] 输入路径含中文和空格时结果正确。

### M3 应用型命令与工具管理界面 → v0.3.0

- [ ] 1. `jobs`：应用部分——端口分配、就绪检测、单实例、宿主退出时清理。
- [ ] 2. 接口：`/api/apps`、`/api/tools/<id>/install|update|rollback|remove`、`/api/config`。
- [ ] 3. 前端：
   - 应用型命令显示「打开」按钮，在新标签页进入工具自带界面。
   - 工具管理：安装进度、一键全部更新、回退到上一版、卸载。
   - 设置页：源列表（目录或 GitHub）、代理。
- [ ] 4. 后台更新检查与 `update-cache.json`。

验收：

- [ ] 点击 perfetto-trim 的「打开」，其界面可用；再点一次不会启动第二个进程。
- [ ] 退出 perfbox 后没有残留的 `perfetto-trim` 进程。
- [ ] 断网启动，界面在一秒内可用，工具状态显示为本地已装版本。
- [ ] 目录源里放一个更高版本，界面提示有更新；更新后再回退，两个方向都正确。
- [ ] 清单 `schema` 为 2 的版本被拒绝，界面提示需要升级 perfbox。

### M4 接入其余五个工具 → v0.4.0

逐个接入，每接入一个，工具仓库和本仓库各提交一次。每个工具的步骤见 5.1 节，命令与参数见附录 A。

- [ ] 1. blkscope：仓库已在 GitHub，只缺 release。
- [ ] 2. vmscope。
- [ ] 3. meminfo_diff。
- [ ] 4. jank-100ms-analysis：第一个 zip 产物，验证 `entry` 列表和去顶层目录。
- [ ] 5. dynamic_model_tools：目录型 zip，验证 `output.fresh`。

宿主侧只有两件事：往 `catalog.json` 加条目；修掉接入过程中暴露的清单表达力不足（改规范要先改 `tool-manifest.md`）。

验收：

- [ ] 七个工具都能从界面安装。
- [ ] 每个工具用一个真实样本跑通，主报告能打开。找不到样本的工具记入第 11 节。
- [ ] 每个工具仓库的契约测试通过。

### M5 打包与分发 → v0.5.0

- [ ] 1. `scripts/perfbox_entry.py`、`scripts/build_exe.ps1`、`build.bat`：PyInstaller 6.17.0 单文件，静态资源打入，结束时做 `--version` 自检。做法照 perfetto-trim 的 `build.bat`。
- [ ] 2. `perfbox export <dir>`：把已装工具写成目录源布局，用于共享盘和离线分发。
- [ ] 3. 宿主新版提示：查 `releases/latest` 的重定向目标，与 `__version__` 比较，界面给出下载链接。
- [ ] 4. `README.md`：面向同事的使用说明——下载、首次运行、结果在哪里、配置共享盘源、离线使用。
- [ ] 5. 创建 perfbox 的 GitHub release，附件为固定文件名 `perfbox.exe`。

验收：

- [ ] `perfbox.exe` 在 PATH 中没有 Python 的环境下运行正常（用清空 PATH 的子 shell 验证）。
- [ ] exe 与源码运行的行为一致：安装、任务、应用、取消各走一遍。
- [ ] 导出的目录设为源后，在全新的 `PERFBOX_HOME` 下七个工具都能安装。
- [ ] 准备同事机器验收的步骤，记入第 11 节。

试用稳定后发 v1.0.0。

### M6 之后（按需，不在本次执行范围内）

- **结构化进度**：约定 stdout 上的进度行，界面显示阶段和百分比。
- **跨工具流转**：例如把 perfetto-trim 裁出的 trace 直接送进 blkscope。
- **静态页面型命令**：dynamic_model_tools 的 `web/` 下两个纯浏览器工具。
- **一个仓库多个产物**：blkscope 的 `startup_firstframe_io.exe`。
- **GitHub Actions 构建发版**：目前各工具都在本机构建。

## 5. 各工具仓库的改动

### 5.1 每个工具的接入步骤

1. 读该仓库的 `AGENTS.md`（没有的读 `README.md` 和构建脚本）。
2. 根目录加 `perfbox-tool.json`，`version`、`tag`、`artifact.name`、`artifact.size`、`artifact.sha256` 留空。内容按附录 A，并对照该工具当前的 argparse 定义逐项核实。
3. 加 `scripts/stamp_manifest.py`（从本仓库 `tools/` 拷贝），构建脚本在产物生成后调用它，写出 `dist/perfbox-tool.json`。
4. 加契约测试：清单里的每个 `flag` 和 `args` 中的子命令都存在于 argparse 定义中。为此把构造 parser 的代码放进一个可单独调用的函数（memscope 和 meminfo_diff 已有 `build_parser()`）。
5. 检查 `tool-manifest.md` 第 7 节的五条运行要求，不满足的在工具侧修。
6. 跑该仓库自己的测试。
7. 把「发版时上传产物和 `dist/perfbox-tool.json`」写进该仓库的 `AGENTS.md` 或 `README.md`。
8. 提交，按 5.2 节发版。
9. 本仓库 `catalog.json` 加条目，用真实 release 验证安装和运行。

### 5.2 发版规则

| 情况 | 工具 | 做法 |
|---|---|---|
| GitHub 上已有 release | memscope、perfetto-trim | 不重新构建，不升版本号。把 release 上的产物下载回来，以它为准计算 sha256 生成清单，作为新附件上传到该 release。不要用本地 `dist\` 里的文件算，它不一定与已发布的逐字节相同 |
| 当前版本号还没有 tag | blkscope 0.6.0、vmscope 0.4.0、meminfo_diff 0.1.1 | 用当前版本号发第一个 release。tag 打在接入提交上 |
| 当前版本号已有 tag | jank 2.1.0、dynamic_model_tools 1.5.0 | 升修订号（2.1.1、1.5.1）后发版 |

- memscope 的规则是「用户可见行为或打包有变化就升版本号」。补传清单时 exe 逐字节不变，所以不升；下一次正常发版时清单自动带出。
- 上传到已有 release 只新增附件，不覆盖、不删除已有附件。
- 发版命令用 `gh release create` 或 `gh release upload`，需要代理时只对当次命令设 `HTTPS_PROXY`。

### 5.3 公开前检查

vmscope、meminfo_diff、jank-100ms-analysis、dynamic_model_tools 四个仓库要新建为 public。公开的是全部提交历史，不只是当前文件，所以推送前检查整个历史：

- 列出历史上出现过的全部文件（`git log --all --name-only`），重点看数据文件、压缩包、截图、日志。
- 在全部历史中搜索内部主机名、内网地址、账号、口令、token。
- dynamic_model_tools 已知跟踪了 `data/` 下的一份真实测试表格（约 2 MB 的 `.xls`）和 `verification/` 下的 40 个文件（多为界面截图）。这些内容是否适合公开，推送前必须问用户。
- 发现任何拿不准的内容都停下来问用户，不要自行改写历史。

其余三个仓库目前跟踪的都是源码、测试和文档，未见数据文件，但仍要按上面的步骤过一遍历史。

### 5.4 各工具的额外事项

| 工具 | 额外事项 |
|---|---|
| memscope | 两个命令共用一套参数；阈值类选项全部标为高级 |
| perfetto-trim | 先只暴露应用型命令 `ui`；`cut`、`split`、`find` 依赖交互选点，留在它自己的界面里 |
| blkscope | 产物用 `blkscope-<ver>-windows-x64.zip`。未跟踪文件 `scripts/whitelist.txt.txt` 是用户的，不要删除也不要提交；`build_release.py` 有工作区干净检查，用 `--allow-dirty` |
| vmscope | 结果在 `-o` 下按输入名分子目录 |
| meminfo_diff | `-o` 传目录 |
| jank-100ms-analysis | 当前 `origin` 指向本地目录，保留它，另加一个名为 `github` 的 remote。`entry` 取自 `run.cmd` |
| dynamic_model_tools | `output.fresh` 为 `true`。先做 5.3 的检查 |

## 6. 测试策略

- pytest，测试不联网、不依赖任何真实工具。先写测试再写实现。
- **manifest**：纯函数，表驱动测试。覆盖每种参数类型、位置参数排序、`multiple`/`count`、未改动参数不传、`output` 三种形态、各类校验错误。
- **sources**：`DirSource` 用临时目录。`GitHubSource` 对一个本机 `ThreadingHTTPServer` 测试（把 `base_url` 指过去），覆盖重定向、404、中途断开、超时。
- **store**：用 `DirSource` 加临时 `PERFBOX_HOME`。覆盖校验失败不留残余、zip 路径穿越被拒、去顶层目录、回退、兼容闸门。只检查磁盘状态，不执行产物。
- **jobs**：直接构造 `InstalledTool`，`entry` 设为 `[sys.executable, 假工具脚本]`。假工具脚本按参数决定行为：写结果文件、持续输出日志、睡眠（测取消）、再启动一个子进程（测进程树清理）、以非 0 退出、输出非 UTF-8 字节。工作目录用临时目录，覆盖结果目录位置、`fresh`、不可写。
- **server**：照 `perfetto-trim/tests/unit/test_web.py`。覆盖 token 与 Host 拒绝、路径穿越、从提交任务到读取结果的全流程。
- **联网冒烟**：标记为 `net`，默认跳过。对真实的 release 跑一次安装。
- **界面**：每个里程碑结束时用全局 Python 的 playwright 截图自查，它不是项目依赖。

## 7. 风险与验证点

| 风险 | 何时验证 | 应对 |
|---|---|---|
| 冻结后的工具在管道下不按 UTF-8 输出，日志出现乱码 | M1 逐个工具实测 | 宿主解码时 UTF-8 失败则回退到系统代码页；同时在工具侧补 `reconfigure(encoding='utf-8')`（meminfo_diff 已有） |
| 程序下载的未签名 exe 被公司杀毒软件拦截 | 需用户在同事机器上验证 | 不能解决则同事一律走共享盘目录源，GitHub 源只留给自己用 |
| 公司网络访问 GitHub 不稳定 | 需用户在公司网络下验证 | 支持代理；失败快速返回并回落到已装版本；主推目录源 |
| 取消任务后留下孤儿进程 | M2 | Job Object；不可用时退回 `taskkill /T /F` |
| 宿主被强制结束，应用进程残留 | M3 | Job Object 设为句柄关闭即结束全部进程 |
| 清单与工具真实命令行不一致 | 每个工具接入时 | 工具仓库内的契约测试 |
| 工具发了不兼容的新清单，旧宿主拉到后出错 | M3 | `schema` 与 `min_perfbox` 闸门；保留上一版可回退 |
| 大包分析占满系统盘临时空间 | M2 | 并发上限缺省为 1；清单里暴露 `--tmpdir` |
| 超长路径或 UNC 路径 | M4 | 宿主原样传递路径；任务标识保持简短 |
| 工作目录是下载目录之类的地方，结果散落 | M5 | README 说明结果位置；界面始终显示工作目录和每个任务的结果目录 |

### 7.1 管道输出编码实测（M1，2026-11-13）

在中文 Windows（ANSI 代码页 936）上，把已发布的 `memscope-0.8.1.exe` 和 `perfetto-trim.exe`（1.3.0）的 stdout/stderr 重定向到管道，取原始字节：

| 工具与命令 | 编码 | 实时性 |
|---|---|---|
| memscope `--help`、`static compare` | cp936，不是 UTF-8（如 stderr 第 38 字节 `0xbc`） | 进度在 stderr，逐行到达；stdout 的结尾几行进程退出时才到 |
| memscope `-V` | 纯 ASCII | — |
| perfetto-trim `--help` | cp936 | — |
| perfetto-trim `ui --no-browser` | 就绪行 `已启动：` 是 GBK 字节，URL 部分是 ASCII | 约 1 秒内到达（`flush=True`） |

结论：

- 两个工具在管道下都按系统代码页输出，不满足 `tool-manifest.md` 第 7 节第 5 条。原因：memscope 的 `cli.main()` 只做了 `reconfigure(errors='replace')`，没有指定 `encoding`；perfetto-trim 没有做 reconfigure。
- 已发布的 exe 不重新构建（5.2 节），所以**宿主必须兼容**：jobs 读日志时按行解码，先试 UTF-8，失败则用系统 ANSI 代码页（`locale.getencoding()`），再失败才用 `errors='replace'`。M2 的 jobs 测试要覆盖 UTF-8、GBK 和非法字节三种行。
- `ready.url_regex` 只匹配 ASCII 部分，不受编码影响。
- 工具侧的修正（memscope 补 `encoding='utf-8'`，perfetto-trim 在 `main()` 里加同样的 reconfigure）随各自下一次正常发版进行，不为此单独发版。M4 接入的工具在接入时一并检查。

## 8. git 管理

**本仓库**

- 分支：只有 `main`，小步直接提交。一个提交只做一件事，测试与实现同一个提交。
- 提交信息：英文一句话标题，需要时正文说明原因，结尾附 Devin 署名行。
- 版本：唯一来源 `src/perfbox/__init__.py` 的 `__version__`。1.0 之前，里程碑升次版本号，修复升修订号。
- tag：附注 tag `vX.Y.Z`，打在改版本号的提交上。
- 不进仓库的内容：虚拟环境、构建产物、任务输出、下载的工具产物、样本数据。
- 远端：`origin` 为 `thomascqdx/perfbox`（public）。

**工具仓库**

- 每个工具的接入改动在它自己的仓库里提交，遵守它自己的 `AGENTS.md`。
- 清单规范的改动先在本仓库提交，再改各工具仓库。

**推送**

- 规则仍是「未经用户明确要求不推送」。用户已同意的范围见第 9 节，执行会话的指令里会再次列明。

## 9. 已确认的决定

2026-11-13 用户确认：

1. 创建 `thomascqdx/perfbox`（public）并推送。
2. vmscope、meminfo_diff、jank-100ms-analysis、dynamic_model_tools 都可以公开。公开前仍按 5.3 节检查，发现疑似内部数据要问。
3. 分析结果放在工作目录下（3.2 节），不放在 `%LOCALAPPDATA%`。
4. 为 memscope v0.8.1 和 perfetto-trim v1.3.0 的现有 release 补传清单。

## 10. 执行方式

- 按里程碑顺序做，一个里程碑的验收全部通过后再进入下一个。
- 第 4 节的勾选框是进度记录。完成一项就勾上，和该项的改动一起提交。新会话接手时，从第一个未勾选的项开始。
- 规范不够用时，先改 `tool-manifest.md` 并提交，再改代码。术语有变化时同步改 `CONTEXT.md`。
- 需要用户本人完成的验证不阻塞进度：把操作步骤写进第 11 节，继续往下做。

必须停下来问用户的情况：

- 5.3 节的检查发现疑似内部数据。
- 需要删除不是自己刚创建的文件、改写 git 历史、覆盖或删除已有的 release 附件。
- 需要推翻 ADR 或第 9 节的决定。
- 某个工具自身的构建或测试失败，且不是接入改动引起的。

## 11. 待用户验证

执行过程中把需要用户本人完成的验证记在这里，每项写明操作步骤和期望结果。

### 11.1 同事机器上的杀毒软件（M1 准备）

目的：确认程序下载的未签名 exe 不会被公司杀毒软件拦截或隔离。

1. 在一台装有公司标准杀毒软件的同事机器上，从 `https://github.com/thomascqdx/perfbox/releases/latest` 下载 `perfbox.exe`（M5 之前可改用源码：`pip install` 后用 `perfbox` 命令）。
2. 新建一个空目录，例如 `D:\pbtest`，在其中打开命令行，执行 `perfbox run memscope -- -V`。
3. 再执行 `perfbox run perfetto-trim -- --version`。
4. 打开杀毒软件的隔离区和事件日志，查看这段时间的记录。

期望：两条命令分别打印 `memscope 0.8.1` 和 `perfetto-trim 1.3.0`；`%LOCALAPPDATA%\perfbox\tools\` 下有两个工具的版本目录，exe 未被删除；杀毒软件没有拦截、隔离或弹窗记录。

如果被拦截：记下杀毒软件名称和提示原文。应对方案是同事一律使用共享盘目录源（`perfbox export` 导出，在 config.json 中配置 `{"type": "dir", "path": "\\\\..."}`），并向 IT 申请对该共享目录加白。

### 11.2 公司网络访问 GitHub（M1 准备）

目的：确认公司网络下 GitHub 源可用，以及不可用时的表现可接受。

1. 在公司网络下（不开个人代理），用一个空的数据目录：`set PERFBOX_HOME=%TEMP%\pb-net`，然后执行 `perfbox list`，记下耗时。
2. 执行 `perfbox install memscope`，观察下载进度和总耗时。
3. 如果公司有 HTTP 代理，在 `%PERFBOX_HOME%\config.json` 中写入 `{"proxy": "http://<公司代理>:<端口>"}`，重复第 2 步。
4. 装好后拔网线或断开 Wi-Fi，执行 `perfbox run memscope -- -V`。

期望：第 1 步在 1 分钟内结束，每个工具显示版本号或「检查失败」，不会卡住；第 2 步（或第 3 步）安装成功；第 4 步立即打印版本号，不出现网络错误。

如果第 2、3 步都失败：记下错误原文。应对方案同 11.1，主推共享盘目录源。

参考：本机测试时，不经代理直连 GitHub 下载 10 MB 的产物在约 50% 处超时；经代理一次成功，约 35 秒。

## 附录 A 各工具的命令与参数

依据 2026-11-13 各仓库的 argparse 定义整理，是写清单的起点。写清单时以工具当时的代码为准，逐项核实。

约定：

- 「位置」表示位置参数，按表中顺序排列。
- 不出现在表里的选项（`--version`、`--open`、`-j` 等）不进清单。
- 阈值类选项一律标为高级；默认值不写进清单，由工具自己决定。

### memscope

两个任务型命令，参数完全相同：

| 命令 | `args` |
|---|---|
| `static-compare` 静态内存包对比 | `["static", "compare"]` |
| `dynamic-compare` 动态内存包对比 | `["dynamic", "compare"]` |

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| base | 位置 | path，文件或目录，`.zip` `.rar` | 基线 A，必填 |
| target | 位置 | path，文件或目录，`.zip` `.rar` | 对比 B，必填 |
| labels | `--labels` | string，`count: 2` | 两侧标签 |
| model | `--model` | choice：`auto` `2.0` `3.0`，`multiple` | 体验模型，填 1 个两侧同用，填 2 个对应 A、B；高级 |
| platform | `--platform` | choice：`auto` `mtk` `unisoc` `qcom`，`multiple` | 芯片平台，规则同上；高级 |
| tmpdir | `--tmpdir` | path，目录 | 临时解压目录；高级 |
| whitelist | `--whitelist` | path，文件 | 自研白名单；高级 |
| prefixes | `--prefixes` | path，文件 | 自研包名前缀；高级 |
| google_prefixes | `--google-prefixes` | path，文件 | Google 包名前缀；高级 |
| 阈值 | `--gap-abs` `--gap-pct` `--resident` `--leak-delta` `--leak-pct` `--leak-r2` `--sys-leak-delta` `--rate-pct` | number | 各一个参数；高级 |

- `output`：`-o`，目录。
- `outputs`：`**/report.html`（主报告）、`**/report.md`、`**/data.xlsx`。

### perfetto-trim

一个应用型命令 `ui`：

- `args`：`["ui", "--port", "{port}", "--no-browser"]`。
- `ready.url_regex`：`http://127\.0\.0\.1:\d+/#t=\S+`。启动时它在 stdout 打印形如 `Perfetto Trim 1.3.0 已启动：http://127.0.0.1:PORT/#t=TOKEN` 的一行。

### blkscope

一个任务型命令 `analyze`：

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| inputs | 位置 | path，文件或目录，`multiple` | trace 或 zip；目录只扫描第一层。扩展名以 `loader.resolve_inputs` 实际接受的为准。必填 |
| json | `--json` | boolean | 输出 `<name>.payload.json`，不再输出 HTML；高级 |

- `output`：`-o`，目录。
- `outputs`：`*.html`（主报告）、`*.payload.json`（数据）。
- 产物是 zip，`entry` 为 `["blkscope.exe"]`。

### vmscope

一个任务型命令 `analyze`：

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| inputs | 位置 | path，文件或目录，`.zip` `.rar`，`multiple` | mobilelog 目录或压缩包，必填 |
| focus | `--focus` | string | 重点启动：包名、时间、`包名@时间` 或 `none` |
| pre_window | `--pre-window` | number | 重点启动前观察窗口，秒 |
| bucket | `--bucket` | number | 分时段统计粒度，分钟 |
| tz | `--tz` | string | 设备时区 `±HH:MM`；高级 |
| watermark | `--watermark` | string | zone 水位 `MIN,LOW,HIGH`，MB；高级 |
| swap_total | `--swap-total` | number | swap 总量，MB；高级 |
| include_last_kmsg | `--include-last-kmsg` | boolean | 纳入 `last_kmsg*`；高级 |
| no_xlsx | `--no-xlsx` | boolean | 不生成 `data.xlsx`；高级 |
| json | `--json` | boolean | 另存 `data.json`；高级 |
| tmpdir | `--tmpdir` | path，目录 | 临时解压目录；高级 |

- `output`：`-o`，目录。每个输入在其下建一个子目录。
- `outputs`：`**/report.html`（主报告）、`**/summary.md`、`**/data.xlsx`、`**/data.json`。

### meminfo-diff

一个任务型命令 `compare`：

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| base_dir | 位置 | path，目录 | Base 版本测试数据目录，必填 |
| target_dir | 位置 | path，目录 | 目标版本测试数据目录，必填 |
| base_name | `--base-name` | string | Base 显示名称 |
| target_name | `--target-name` | string | Target 显示名称 |
| recursive | `-r` | boolean | 强制递归扫描子目录 |
| json | `--json` | boolean | 同时导出同名 `.json`；高级 |

- `output`：`-o`，目录。
- `outputs`：`*.html`（主报告）、`*.json`（数据）。
- 不带参数且 stdin 是终端时它会进入交互选择目录；宿主总是带参数并关闭 stdin，不会触发。

### jank-analyzer

一个任务型命令 `analyze`：

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| directory | 位置 | path，目录或 `.txt` 文件 | ZIP/TRACE 目录，或文件名清单，必填 |
| file_list | `--file-list` | path，文件，`.txt` | 文件名清单 |
| package | `--package` | string | 只分析指定包名 |
| min_duration_ms | `--min-duration-ms` | number | 帧耗时下限 |
| max_duration_ms | `--max-duration-ms` | number | 帧耗时上限 |
| 阈值 | `--running-threshold-ms` `--doframe-running-threshold-ms` `--doframe-wall-threshold-ms` | number | 各一个参数；高级 |

- `output`：`--output`，`file` 型，`filename` 为 `jank_analysis.md`。
- `outputs`：`*.md`（主报告）、`*.csv`（数据）。
- 产物是 zip，`entry` 为 `["runtime/python.exe", "-B", "-X", "utf8", "{root}/analyze_jank.py"]`。
- `--trace-processor` 和 `--offline` 不暴露；便携包自带 Trace Processor 且始终离线。

### startup-latency

一个任务型命令 `batch`：

| 参数 | 选项 | 类型 | 说明 |
|---|---|---|---|
| input | 位置 | path，文件或目录，`.xls` `.xlsx`，`multiple` | 测试结果表格或目录（递归），必填 |
| app_list | `--app-list` | path，文件 | 包名名单，只分析名单内的应用 |
| batch | `--batch` | string | 测试批次 |
| exclusions | `--exclusions` | path，文件，`.xlsx` | 用户编辑过的排除台账 |

- `output`：`--output`，目录，`fresh: true`（目录已存在时工具会报错）。
- `outputs`：以 `latency_batch` 的 `write_report` 和 `export_workbooks` 实际写出的文件为准，跑一次样本后填写。已知有 `analysis.json` 和 `images/`。
- 产物是 zip，解压后入口为 `startup-latency-consistency-analyzer.exe`。
- 不带参数时它会进入交互模式；宿主总是带输入路径，不会触发。
