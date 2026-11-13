# 清单随工具发版，不放在 perfbox 里

每个工具的命令、参数和输出由工具仓库里的 `perfbox-tool.json` 声明，并以固定文件名作为 release 附件发布。perfbox 自身只内置工具名录（有哪些工具、去哪里找），不内置任何工具的参数定义。

原因：perfbox 默认拉取各工具的最新版。如果参数定义写在 perfbox 里，工具发新版改了命令行，已分发出去的 perfbox 就会生成错误的调用，而且只能靠重新分发 perfbox 来修。清单跟着工具版本走，参数表和它描述的那个 exe 永远是同一次发版的产物。

## Consequences

- 七个工具仓库都要新增清单文件，并在发版流程里上传它。
- 固定文件名 `perfbox-tool.json` 让 perfbox 可以用 `releases/latest/download/perfbox-tool.json` 一次请求拿到最新版本号、产物文件名和 sha256，不经过 GitHub API，因此不受匿名限流（每 IP 每小时 60 次）影响，也不要求各工具统一产物命名。
- 清单带 `schema` 版本号。perfbox 遇到不认识的 `schema` 时拒绝安装该版本并保留已装版本，所以清单格式的不兼容变更必须先发 perfbox、后发工具。
- 清单可能与工具真实命令行不一致。靠各工具仓库内的契约测试（清单里的每个 flag 都必须存在于 argparse 定义中）来防。
