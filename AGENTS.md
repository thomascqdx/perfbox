# perfbox 开发说明

perfbox 是性能分析工具箱的宿主：获取、运行、管理各独立工具，自身不含分析逻辑。

## 先读

- `CONTEXT.md`：术语表。代码、文档、界面文案一律使用其中的术语。
- `docs/implementation-plan.md`：架构、里程碑、各工具仓库的改动清单。第 4 节的勾选框是进度记录，完成一项勾一项。
- `docs/tool-manifest.md`：`perfbox-tool.json` 规范，工具接入的唯一依据。
- `docs/adr/`：不可轻易推翻的决策及原因。

## 硬性约束

- **运行时只用 Python 标准库**。`src/perfbox/` 不得 import 第三方包；第三方包只允许出现在 `tests/` 和 `scripts/`。
- **不放任何工具的分析代码或参数定义**。工具的参数只来自它自己发布的清单（见 ADR 0002）。
- **只监听 127.0.0.1**。
- **结果与宿主数据不混放**。任务结果放在工作目录下的 `out\<任务标识>\`；宿主自己的数据（工具库、任务记录、日志、配置）放在 `%LOCALAPPDATA%\perfbox`。
- 前端用原生 HTML/CSS/JS，不引入构建步骤。

## 环境

- Windows，Python 3.13（`py -3.13`），虚拟环境放在项目内 `.venv`。
- 默认 shell 是 Windows PowerShell 5.1：
  - 不支持 `&&`，用 `; if ($?) { ... }`。
  - 不要把含中文的脚本通过管道喂给 `python -`，中文会变成 `?`。先写成 UTF-8 文件再运行。
  - `Set-Content` 没有无 BOM 的 UTF-8 选项，含中文的文件不要用它写。
- 联网失败时用本机代理 `http://127.0.0.1:7890`：pip 加 `--proxy`，其他命令设 `$env:HTTPS_PROXY` 且只对当次命令生效。不要改全局 git 或 pip 配置。

## 同级的工具仓库

工具仓库与本仓库同级，位于 `D:\codes\sagereal\`：`memscope`、`perfetto-trim`、`blkscope`、`vmscope`、`meminfo_diff`、`jank-100ms-analysis`、`dynamic_model_tools`。

- 改动工具仓库前先读它自己的 `AGENTS.md`，各仓库的版本号规则和提交约定不同。
- `perfetto-trim/src/pftrim/web/server.py` 是本机 Web 服务的参考实现（token、Host 校验、后台任务、原生文件对话框）。照它的做法写，不要 import 它。

## git

- 分支 `main`，直接在 `main` 上小步提交，一个提交只做一件事。
- 提交信息：英文一句话标题，需要时正文说明原因，结尾附 Devin 署名行（照抄 `git log` 中已有格式）。
- 多行提交信息先写到临时文件，再 `git commit -F <file>`。
- 版本号唯一来源是 `src/perfbox/__init__.py` 的 `__version__`。每个里程碑完成后打附注 tag `vX.Y.Z`，打在改版本号的那个提交上。
- 未经用户明确要求不要 push，包括 tag。
- 向任何工具仓库的 GitHub Release 上传文件，或创建新的 GitHub 仓库，都要先得到用户明确同意。已同意的范围见 `docs/implementation-plan.md` 第 9 节。
