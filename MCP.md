# Nerya：外部 Agent、MCP、Skill 与 OpenAI Tunnel

CLI 和 MCP 共用工具目录、真实 JSON Schema、输入校验和现有业务处理器。MCP 默认关闭；本地 CLI 不需要开放网络，也不需要 MCP SDK。

## 设置页

入口：**设置 → MCP 与外部 Agent**（`/settings#mcp`）。提供中英文界面。

可以在这里开启/关闭 MCP、设置公网域名、复制接口地址、配置外部写权限及工具允许/拒绝列表、撤销全部 OAuth 授权、浏览/管理 Skill，以及保存和连接 OpenAI Secure MCP Tunnel。

前置条件：

1. 在「设置 → 访问」中设置 Nerya 管理员密码。
2. 在实际运行后端的 Python 环境中安装 MCP 可选依赖。产品目录为 `agent/`：

```sh
uv sync --extra mcp
# 或在实际运行后端的 Python 环境中：
python -m pip install -e '.[mcp]'
```

安装后重启后端与 dashboard，使新路由和设置页生效。设置页会显示缺少 SDK、管理员密码、Tunnel 客户端等前置条件，不会假装已经连接。

保存设置使用 revision 校验，旧表单不会覆盖新设置。API key 在保存后不回显；留空保留已保存凭证。每次修改 MCP 开放范围会撤销现有 OAuth 授权，外部客户端需要重新登录。

## 同一个公网 URL

MCP HTTP 路由挂在已有 Nerya API listener 上，dashboard 使用独立的、**不注入管理员凭证**的代理转发 MCP/OAuth 路径。因此公网入口指向 dashboard 或 API 时，不需要再开放一个 MCP 端口。

例如已公开 `https://nerya.example.com`，外部 MCP 地址就是：

```text
https://nerya.example.com/mcp
```

MCP 设置的「公网域名」留空时使用现有活动 tunnel 的已知公网地址。使用自定义反向代理或 tunnel 没有报告域名时，填写精确 HTTPS origin，不含路径。临时 tunnel 更换地址后，客户端应重新发现并授权。多公网入口以这里选择的一个 origin 作为规范 OAuth issuer。

反向代理必须转发以下路径，保留请求 Host、Authorization、Origin、Cookie 和 OAuth 响应 Location/Set-Cookie，不做登录页重写：

```text
/mcp
/mcp/oauth/authorize
/mcp/oauth/token
/mcp/oauth/register
/mcp/oauth/revoke
/mcp/oauth/login
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server/mcp/oauth
/.well-known/oauth-authorization-server
```

OAuth 登录页面只使用配置/已知 tunnel 的域名，绝不根据未经验证的请求 Host 生成 redirect。MCP Host/Origin 防护继续启用。

`mcp.enabled: false` 时集成 HTTP 路径返回 404；启用后未授权 MCP 请求返回带 protected-resource metadata 地址的 401。关闭立即拒绝新请求，但不要假设已经开始的写操作被回滚。修改设置后现有 MCP bridge 会在下一次请求时重新加载。

## OAuth2：使用管理员密码

HTTP 默认是 **authorization code + S256 PKCE**。外部客户端在 Nerya 的登录页输入现有管理员密码并确认授权；密码不交给 chatbot，不用于 Basic Auth，也不是 password grant。

支持动态客户端注册。客户端注册时显式指定公共客户端 `token_endpoint_auth_method: none`，注册精确 redirect URI；允许 HTTPS redirect 和本机 loopback HTTP redirect，不接受 fragment 或 URI userinfo。

```json
{
  "client_name": "My external agent",
  "redirect_uris": ["https://client.example/callback"],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "scope": "mcp"
}
```

资源参数必须与规范 MCP 地址一致：

```text
resource=https://nerya.example.com/mcp
scope=mcp
code_challenge_method=S256
```

实现包括一次性授权码、CSRF nonce + HttpOnly consent cookie、精确 redirect/resource 校验、有限期 access token、refresh token 轮换和重放撤销、登录/注册频率限制，以及管理员端全部授权撤销。

授权码有效期 2 分钟，access token 1 小时，refresh token 30 天。授权码与 token 在工作区 SQLite 中仅以 SHA-256 摘要作索引，不保存明文。管理员密码修改、关闭 MCP 或在设置中撤销授权都会使相关 token 失效。

OAuth token 与 dashboard JWT 完全分开；本机 loopback 请求也不会自动获得 MCP 管理员身份。当前是一个 Nerya 工作区的管理员授权模型，不是多用户 RBAC 或多租户账号系统。

## 外部写权限

默认只读。设置中的两个写入开关分别控制：

- **策略与 Skill 提案**：允许策略生成和 `nerya_skill_manage`。UI 开启时会将 `nerya_trigger_emit` 加入拒绝列表，避免误开放运行事件触发。
- **Agent 与配置管理**：授权已有原生角色管理工具及配置提案。默认管理目录不包含 shell、实盘提升、下单或 vault 导出。

全局 `mcp.allow_tools` / `mcp.deny_tools` 使用完整公开工具名，作用于所有工具层。null allowlist 表示没有额外限制；空列表表示零工具。allowlist 不会提升底层权限。外部 agent 不能通过配置提案修改 MCP 开放范围。

## 全量 Skill 读取和管理

新增共享工具：

| 工具 | 用途 |
| --- | --- |
| `nerya_skills_catalog` | 列出所有 Skill 定义，包括嵌套 Skill 和已禁用 Skill；支持分页、搜索和 scope。 |
| `nerya_skill_read` | 读取 SKILL.md、references、templates、scripts、tests 中的 UTF-8 文本，支持分页，返回 revision。 |
| `nerya_skill_manage` | 创建/更新/删除 Skill 内容，或提议启用/禁用及 Agent 分配变更。 |

`scope` 可以是 `all`、`builtin`、`workspace`、`agent`。Agent scope 需要现有角色名 `agent_id`。`include_unassigned: true` 可以在 Agent 目录中同时显示未分配 Skill，方便增加分配。

Agent 沿用 Nerya 的真实模型：**共享 Skill 定义 + 每个角色的 allowed_skills**。Agent scope 管理分配；编辑共享说明或脚本请使用 all/Workspace scope，避免伪造不被运行时消费的私有目录。

内置及用户级全局 Skill 是只读源。编辑会创建 Workspace override；已有 override 必须先从 Workspace scope 读取后再编辑。内置 Skill 不可通过这个入口删除，可禁用。Workspace Skill 可以创建、编辑、删除。读取禁用的 Skill 不等于授权执行它。

所有写操作仅生成 `pending_review` 提案：带候选快照/冲突检查、静态校验报告及证据，审批后由现有 promotion/rollback 链路应用。静态校验检查范围、文本、YAML/Python 语法，不执行拟议脚本，也不宣称验证了其交易效果。

内容修改和删除携带 `skill_read.revision`；新建使用 `revision: missing`。Workspace enable/disable 携带目录的 `enabled_revision`；Agent 分配携带 `binding_revision`。显式空的 enabled/allowed_skills 列表表示不启用/不分配，不回退成全部权限。

文件上限 512 KiB，单次返回最多 32,000 字符，提案总文本最多 2 MiB。禁止绝对路径、父目录跳转和 symlink，敏感文本先脱敏再分页。二进制附件不作为文本输出。不要把脱敏内容写回完整文件。

## CLI

```sh
nerya tools list --workspace /absolute/workspace
nerya tools describe nerya_skills_catalog --workspace /absolute/workspace
nerya tools call nerya_skills_catalog --workspace /absolute/workspace \
  --input '{"scope":"all","limit":100}'
nerya tools call nerya_skill_read --workspace /absolute/workspace \
  --input '{"skill_id":"research","file":"SKILL.md"}'
nerya tools call nerya_skills_catalog --workspace /absolute/workspace \
  --input '{"scope":"agent","agent_id":"market_analyst","include_unassigned":true}'

nerya agent list --workspace /absolute/workspace
nerya agent show market_analyst --workspace /absolute/workspace
nerya config get agents.yml --workspace /absolute/workspace

# 显式授权对应写权限后：
nerya tools call nerya_skill_manage --workspace /absolute/workspace --input-file skill-change.json
nerya tools call nerya_strategy_generate --workspace /absolute/workspace --input-file strategy.json
nerya agent save --workspace /absolute/workspace --input-file role.json
nerya config propose --workspace /absolute/workspace --input-file config-proposal.json
```

机器接口 stdout 输出一个 JSON 值，诊断走 stderr。退出码 0 成功、1 工具/初始化失败、2 CLI/JSON 输入错误。`--input-file -` 读取 stdin，输入最大 1 MiB，禁止 NaN/Infinity。`nerya mcp list-tools` 返回同一目录。

`nerya_strategy_generate` 使用已有生成器；不传 files 使用模板，传入 package-relative files 则校验外部 agent 的实现。不在此接口发起 LLM 调用，不自动上线或交易。配置提案中的 `config_after` 仍是完整文档替换，不是 merge patch。

## 本机 stdio 和独立 HTTP 兼容入口

本机客户端可以通过 `nerya mcp serve --workspace /absolute/workspace --transport stdio` 启动子进程；仍需 `mcp.enabled: true`。stdio 不监听网络，使用本机进程权限边界，不弹 OAuth 登录。

`nerya mcp serve --transport http` 保留独立监听模式，默认 OAuth2；需要管理员密码和明确公网域名/本机 loopback origin。旧静态 Bearer 行为仅在独立进程显式配置 `mcp.auth_mode: bearer` 时保留，凭证来自 mcp.token_env。集成公网入口及设置页只开放 OAuth2。

## OpenAI Secure MCP Tunnel

设置页支持保存 **Tunnel ID**（`tunnel_...`，API 字段 `tunnel_id`）和运行时 API key，开启/断开连接，显示安装状态、进程状态与 `/readyz` 就绪状态，并提供 Responses API 工具配置。

后端主机需要安装 OpenAI 官方客户端：

```sh
brew install openai/tools/tunnel-client
```

这条命令是 macOS 安装示例；其他系统按 OpenAI 官方发布安装。设置页不会自动执行 shell 安装操作。

使用有权访问对应 tunnel 的 runtime API key；API key 进入现有加密 Vault，仅在启动子进程时通过 `CONTROL_PLANE_API_KEY` 传入，Tunnel ID 通过 `CONTROL_PLANE_TUNNEL_ID` 传入，不把 key 放到命令行或日志。Vault 必须具有有效 passphrase。

官方客户端连接本机 API 的 `/mcp`。启动器设置 `MCP_OAUTH_TRUSTED_ORIGINS` 为配置的精确公网 origin，允许获取独立公网 OAuth 元数据，不开放任意额外域名。仅终止本管理器持有的子进程，不扫描或终止其他 tunnel-client。

启用后保存设置，再点「连接」。保持 enabled 时，Nerya API 重新启动会尝试恢复连接；「断开」会保存为 disabled，避免下次自动连接。修改设置会停止旧连接，保存后再连接。只有 `/readyz` 成功才显示已就绪；进程存在不等于 Tunnel 已可用。

Responses API 的工具配置形态：

```json
{
  "type": "mcp",
  "server_label": "nerya",
  "tunnel_id": "tunnel_0123456789abcdef0123456789abcdef"
}
```

**Tunnel runtime API key 不代替 MCP OAuth 用户授权。** 外部调用方仍需完成 OAuth。OpenAI Tunnel 可以转发 MCP/OAuth 发现，但授权服务器本身不会自动被公开，因此 Nerya 的登录页面仍需要可被浏览器访问的 HTTPS 公网 origin。不要把 localhost 授权页直接交给远端用户。

官方依据（2026-09-19 核对）：
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://github.com/openai/tunnel-client/blob/master/docs/configuration.md

## 验证与边界

```sh
python -m pytest -q tests/test_external_tools.py tests/test_skill_management.py
# 安装 dev + mcp extra 后：
python -m pytest -q tests/test_external_mcp_protocol.py tests/test_mcp_oauth_settings.py tests/test_mcp_public_bridge.py
cd dashboard
./node_modules/.bin/tsc --noEmit
node scripts/mcp-proxy.test.cjs
```

测试使用临时工作区、内存 HTTP 与本机随机端口，验证 OAuth/PKCE、撤销和重放、Skill 内容/分配提案、默认关闭、public proxy 身份隔离及 Tunnel 子进程参数，不调用真实市场、LLM 或交易。

真实 OpenAI Tunnel 端到端连通性需要操作者提供实际 runtime key、Tunnel ID、公网 TLS/域名并完成 OAuth。本实现阶段没有使用真实凭证连接，不能把参数测试当成外部连通性证明。写请求超时后先检查提案/角色状态，不要盲重试；没有新增通用 exactly-once 承诺。
