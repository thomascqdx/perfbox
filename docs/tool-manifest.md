# 清单规范：perfbox-tool.json（schema 1）

状态：定稿（schema 1），2026-11-13。本文件是各工具仓库接入的唯一依据。

清单随工具每次发版一起发布，文件名固定为 `perfbox-tool.json`。宿主靠它知道：这个版本的产物是哪个文件、怎么校验、有哪些命令、每个命令怎么拼命令行、结果在哪里。术语见仓库根目录 `CONTEXT.md`。

## 1. 发布位置

| 源 | 清单位置 | 产物位置 |
|---|---|---|
| GitHub Release | `https://github.com/<owner>/<repo>/releases/latest/download/perfbox-tool.json` | `https://github.com/<owner>/<repo>/releases/download/<tag>/<artifact.name>` |
| 目录 | `<root>/<id>/<version>/perfbox-tool.json` | `<root>/<id>/<version>/<artifact.name>` |

- GitHub 源的两个地址都是网页重定向，不走 REST API。
- 目录源的「最新版」是 `<root>/<id>/` 下版本号最大的子目录，按数字逐段比较，不是字符串比较。
- 目录源的 `<root>` 可以是本地路径或 UNC 共享路径。

## 2. 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema` | int | 是 | 固定为 `1`。宿主遇到更大的值时拒绝安装该版本 |
| `id` | string | 是 | 工具标识，`[a-z0-9][a-z0-9-]*`。必须与工具名录中的标识一致 |
| `title` | string | 是 | 界面上显示的名称 |
| `summary` | string | 否 | 一句话说明 |
| `domain` | string | 是 | 问题域：`startup`、`jank`、`io`、`memory`、`trace` 之一 |
| `version` | string | 是 | `X.Y.Z` |
| `tag` | string | 否 | release 的 tag，缺省为 `v<version>` |
| `min_perfbox` | string | 否 | 能运行此清单的最低宿主版本 |
| `artifact` | object | 是 | 见第 3 节 |
| `commands` | array | 是 | 至少一个，见第 4 节 |

- 版本号（`version`、`min_perfbox`）格式为 `X.Y.Z`，三段都是非负整数，比较时逐段按数字比较。
- 宿主先检查 `schema` 和 `min_perfbox`，不满足时报「需要升级 perfbox」，不再做其余校验。
- 其余校验是严格的：出现本规范未定义的字段，清单即无效。schema 1 内以后新增可选字段时，用到新字段的清单必须同时声明对应的 `min_perfbox`，旧宿主会先被 `min_perfbox` 拦下，而不是报「未定义的字段」。

## 3. artifact

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 产物文件名，不含路径 |
| `kind` | string | 是 | `exe` 或 `zip` |
| `size` | int | 是 | 字节数 |
| `sha256` | string | 是 | 小写十六进制 |
| `entry` | array | zip 必填 | 启动命令的固定前缀。`exe` 缺省为 `[name]` |

`entry` 的规则：

- 第一个元素是可执行文件，相对于安装目录。
- 其余元素原样作为参数；其中的 `{root}` 替换为安装目录的绝对路径。
- 不允许用 `.cmd` 或 `.bat` 作入口。经 `cmd.exe` 转发参数时，路径里的 `&`、`%`、`"` 会被二次解析。原来靠批处理启动的工具，把批处理里的那行命令直接写进 `entry`。
- zip 解压后如果只有一个顶层目录，宿主会去掉这一层，所以 `entry` 里不要写带版本号的顶层目录名。

`sha256` 只用于发现下载损坏或截断。清单和产物来自同一个源，它不能证明产物的来历；来历靠 HTTPS 和 GitHub 账号保证。

## 4. commands

每个命令的公共字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 工具内唯一 |
| `title` | string | 是 | 界面上显示的名称 |
| `summary` | string | 否 | 一句话说明 |
| `kind` | string | 是 | `job`（任务型）或 `app`（应用型） |
| `args` | array | 否 | 紧跟 `entry` 的固定参数，如 `["static", "compare"]` |
| `params` | array | 否 | 用户可填的参数，见第 5 节 |

### 4.1 任务型（`kind: "job"`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `output` | object | 是 | 宿主如何把结果目录告诉工具 |
| `outputs` | array | 是 | 结果里有哪些文件 |

`output`：

| 字段 | 说明 |
|---|---|
| `flag` | 指定输出位置的选项，如 `-o`、`--output` |
| `kind` | `dir`：传一个目录；`file`：传一个文件路径 |
| `filename` | `kind` 为 `file` 时的文件名，如 `jank_analysis.md` |
| `fresh` | `true` 表示工具要求该目录事先不存在（由工具自己创建）。缺省 `false`，宿主会先建好目录 |

`outputs` 的每一项：

| 字段 | 说明 |
|---|---|
| `glob` | 相对结果目录的匹配模式，支持 `**` |
| `role` | `report`（供人阅读）、`data`（Excel/JSON/CSV）、`trace`（生成的 trace 文件） |
| `primary` | `true` 表示主报告，任务完成后界面默认打开它。至多一项 |

任务成功的判据是退出码为 0。退出码非 0 时，结果目录里已有的文件仍然列出，但任务标记为失败。

### 4.2 应用型（`kind: "app"`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ready` | object | 是 | 如何判断应用已就绪并取得访问地址 |

- `args` 中的 `{port}` 替换为宿主挑选的空闲端口。
- `ready.url_regex`：宿主逐行读取应用的 stdout，第一个匹配到的子串就是访问地址。30 秒内没有匹配视为启动失败。
- 应用必须只监听 `127.0.0.1`，并且不自行打开浏览器。

## 5. params

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 命令内唯一 |
| `label` | string | 是 | 表单上的标签 |
| `help` | string | 否 | 提示文字 |
| `type` | string | 是 | `path`、`string`、`number`、`integer`、`boolean`、`choice` |
| `flag` | string | 否 | 选项名，如 `--tmpdir`。省略表示位置参数 |
| `required` | bool | 否 | 缺省 `false`。位置参数通常为 `true` |
| `multiple` | bool | 否 | 可填多个值（对应 argparse 的 `nargs='+'` 或 `'*'`） |
| `count` | int | 否 | 必须恰好填这么多个值（对应 `nargs=N`） |
| `default` | any | 否 | 仅用于界面显示，见下 |
| `advanced` | bool | 否 | `true` 时收进「高级选项」 |
| `accept` | array | `path` 必填 | `["file"]`、`["dir"]` 或两者 |
| `extensions` | array | 否 | `path` 接受的文件扩展名，如 `[".zip", ".rar"]` |
| `choices` | array | `choice` 必填 | 可选值 |

`default` 只告诉用户「不填时工具会用什么值」。用户没有改动的参数，宿主不会把它传给工具，默认值始终由工具自己决定。这样清单不需要和工具代码里的默认值保持同步。

`boolean` 必须带 `flag`：勾选时传该选项，不勾选时什么都不传。

「未改动」的判定：参数值缺失、为 `null`、为空字符串、为空列表、`boolean` 为 `false`，或与 `default` 相等，都视为未改动，不传给工具。

参数值的形态（界面提交给宿主的 JSON 值）：

| `type` | 单值 | `multiple` / `count` |
|---|---|---|
| `path`、`string` | 字符串，原样传递 | 字符串数组 |
| `number` | 数字，或可解析为数字的字符串 | 同左的数组 |
| `integer` | 整数，或可解析为整数的字符串 | 同左的数组 |
| `boolean` | `true` / `false` | 不允许 |
| `choice` | `choices` 中的一个 | `choices` 中的值组成的数组 |

- `multiple` 与 `count` 不能同时出现。`count` 必须恰好给出 `count` 个值；`multiple` 至少一个。
- 宿主不检查路径是否存在，路径原样交给工具，相对路径按工作目录解析。

## 6. 命令行拼装

宿主按下面的顺序拼出完整命令行，各段之间不做任何 shell 解析：

```
entry  +  args  +  位置参数（按 params 中的顺序）  +  选项参数（按 params 中的顺序）  +  output
```

- `multiple` 或 `count` 的选项参数拼成「一个 flag 后跟全部值」，不重复 flag。
- 位置参数里至多一个可以是 `multiple`，且必须排在位置参数的最后。
- 宿主以自己的工作目录（启动 perfbox 时的当前目录）作为工具进程的当前目录，stdin 关闭。结果目录是 `<工作目录>\out\<任务标识>\`，可在提交任务时改到别处。

## 7. 对工具的运行要求

清单之外，工具本身要满足：

1. **完全非交互**：stdin 不是终端时，不得等待任何输入。
2. **输出位置可指定**：所有结果文件都写进 `output` 指定的位置。
3. **不自行打开浏览器**：宿主不会传 `--open` 之类的选项。
4. **退出码有意义**：0 表示成功，非 0 表示失败。
5. **管道输出用 UTF-8**：stdout 不是终端时按 UTF-8 输出，并且及时 flush，否则界面上看不到实时日志。

## 8. 仓库内的清单与发版

- 仓库根目录签入 `perfbox-tool.json`（称为清单模板），其中 `version`、`tag`、`artifact.name`、`artifact.sha256` 写空字符串 `""`，`artifact.size` 写 `0`。其余字段照常填写；`exe` 产物不写 `entry`。
- 构建产物后运行 `scripts/stamp_manifest.py`（从 perfbox 仓库的 `tools/` 拷贝，只依赖标准库），由它读取版本号和产物文件，写出 `dist/perfbox-tool.json`：

  ```
  python scripts/stamp_manifest.py --version-from <含 __version__ 或 VERSION 的文件> --artifact <产物路径> [--template perfbox-tool.json] [--out dist/perfbox-tool.json]
  ```

  它填入 `version`、`tag`（`v<version>`）、`artifact.name`（产物文件名）、`artifact.size`、`artifact.sha256`，其余内容原样保留。也可以用 `--version X.Y.Z` 直接给出版本号。
- `perfbox manifest check <file>` 校验一份发布用的清单；`perfbox manifest check --template <file>` 校验清单模板（允许上述五个字段为空）。
- 发版时把产物和 `dist/perfbox-tool.json` 一起上传为 release 附件。
- 每个工具仓库加一个契约测试：清单里出现的每个 `flag` 和 `args` 中的子命令，都必须存在于工具的 argparse 定义中。

## 9. 示例

### 9.1 单 exe、任务型（memscope）

```json
{
  "schema": 1,
  "id": "memscope",
  "title": "内存测试包 A/B 对比",
  "summary": "对比两个内存测试包，输出 HTML、Markdown 和 Excel 报告",
  "domain": "memory",
  "version": "0.8.1",
  "tag": "v0.8.1",
  "artifact": {
    "name": "memscope-0.8.1.exe",
    "kind": "exe",
    "size": 10483503,
    "sha256": "由 stamp_manifest.py 填写"
  },
  "commands": [
    {
      "id": "static-compare",
      "title": "静态内存包对比",
      "kind": "job",
      "args": ["static", "compare"],
      "params": [
        {"name": "base", "label": "基线 A", "type": "path",
         "accept": ["file", "dir"], "extensions": [".zip", ".rar"], "required": true},
        {"name": "target", "label": "对比 B", "type": "path",
         "accept": ["file", "dir"], "extensions": [".zip", ".rar"], "required": true},
        {"name": "labels", "label": "两侧标签", "type": "string",
         "flag": "--labels", "count": 2, "advanced": true},
        {"name": "tmpdir", "label": "临时解压目录", "type": "path",
         "accept": ["dir"], "flag": "--tmpdir", "advanced": true}
      ],
      "output": {"flag": "-o", "kind": "dir"},
      "outputs": [
        {"glob": "**/report.html", "role": "report", "primary": true},
        {"glob": "**/report.md", "role": "report"},
        {"glob": "**/data.xlsx", "role": "data"}
      ]
    }
  ]
}
```

### 9.2 应用型（perfetto-trim）

```json
{
  "id": "ui",
  "title": "裁剪 / 切分 / 查找",
  "kind": "app",
  "args": ["ui", "--port", "{port}", "--no-browser"],
  "ready": {"url_regex": "http://127\\.0\\.0\\.1:\\d+/#t=\\S+"}
}
```

### 9.3 zip 产物（jank-analyzer）

原来的 `run.cmd` 内容是 `runtime\python.exe -B -X utf8 analyze_jank.py %*`，直接写成 `entry`：

```json
{
  "name": "jank-analyzer-2.1.0-windows-x64.zip",
  "kind": "zip",
  "size": 16900000,
  "sha256": "由 stamp_manifest.py 填写",
  "entry": ["runtime/python.exe", "-B", "-X", "utf8", "{root}/analyze_jank.py"]
}
```

## 10. 预留，schema 1 不实现

- **结构化进度**：工具在 stdout 输出约定前缀的 JSON 行来报告阶段和百分比。
- **静态页面型命令**：产物里的纯浏览器 HTML 工具，由宿主直接打开。
- **一个工具多个产物**：例如 blkscope 仓库里的 `startup_firstframe_io.exe`。
- **跨工具流转**：根据 `outputs.role` 和其他工具 `params.extensions` 的匹配，提供「发送到…」。
