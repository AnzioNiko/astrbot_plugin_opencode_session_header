# OpenCode 会话请求头注入

> 插件 ID：`astrbot_plugin_opencode_session_header`

为 OpenCode Zen / OpenCode Go 请求自动补充 `x-opencode-session` 请求头的 AstrBot 插件。

- GitHub 仓库：[AnzioNiko/astrbot_plugin_opencode_session_header](https://github.com/AnzioNiko/astrbot_plugin_opencode_session_header)
- 目标端点默认值：`https://opencode.ai/zen/go/v1`
- 运行环境：AstrBot；无需额外安装第三方 Python 依赖

## 功能

插件只处理目标端点的 HTTP 请求，不读取或改写提示词、聊天内容及模型参数。

1. 如果请求已经携带非空的 `x-opencode-session`，保留已有值，不覆盖。
2. 如果能取得 AstrBot 会话 ID（优先 `unified_msg_origin`，其次请求的 `session_id`），根据会话 ID 稳定推导 UUID。相同会话在重启后仍得到相同值。
3. 如果请求上下文没有会话 ID，按“缺失会话兜底方式”生成请求头，避免因缺少会话上下文而跳过注入。
4. 仅当请求的协议、主机和路径属于配置的目标端点时生效。目标路径下的接口路径（如 `/chat/completions`）也会匹配；其他 API 地址保持不变。
5. 管理员可使用 `/oc-session` 查看当前会话对应的 UUID 和端点匹配情况。

## 安装

将整个插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_opencode_session_header`，然后在 AstrBot 中启用插件并重载插件或重启 AstrBot。也可以通过 AstrBot 的插件管理功能从本仓库安装。

## 插件设置

| 设置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 插件总开关。 |
| `target_endpoint` | `https://opencode.ai/zen/go/v1` | 注入请求头所针对的 API 基础端点。 |
| `header_name` | `x-opencode-session` | 请求头名称。 |
| `ignore_trailing_slash` | `true` | 端点匹配时是否忽略末尾的 `/`。 |
| `missing_session_mode` | `random` | 缺少会话 ID 时选择 `random` 或 `fixed`。 |
| `fixed_session_uuid` | 空 | `fixed` 模式使用的 UUID；须为有效 UUID。 |
| `log_injection` | `true` | 是否记录请求头注入日志。 |
| `log_skip` | `true` | 是否记录跳过或保留已有请求头的日志。 |

### 缺失会话时的模式

- **`random`**：插件加载时生成一个随机 UUID，并在当前插件运行期间复用；重启或重载插件后会生成新的值。这避免无会话上下文的连续请求每次都使用不同的 Session。
- **`fixed`**：使用 `fixed_session_uuid` 中填写的 UUID，适合需要跨重启保持同一个兜底 Session 的场景。
- 若选择 `fixed`，但 UUID 留空或格式无效，插件会记录警告并退回 `random` 模式。

有正常的 AstrBot 会话 ID 时，仍使用原有的会话 ID 确定性映射，不受兜底设置影响。设置变更后请重载插件或重启 AstrBot。

## 日志与隐私

插件自身不发起网络请求；实际模型请求仍由 AstrBot 和配置的模型提供商发出。注入日志可能包含 Session UUID 和端点信息，可通过 `log_injection`、`log_skip` 及 AstrBot 全局日志级别控制日志输出。UUID 是会话标识，不是认证凭据或秘密令牌。

## 仓库内容

- `main.py`：插件实现
- `_conf_schema.json`：AstrBot 插件设置定义
- `metadata.yaml`：插件元数据
- `README.md`：使用说明