# Agent MCP 接入（只读）

面向「让 agent 读战队数据」的接入说明。端点内置在门户进程里，不开新服务、
不引第三方 SDK：`POST /mcp`，JSON-RPC 2.0，streamable HTTP，**只读**。

- 端点：`https://<门户域名>/mcp`
- 鉴权：`Authorization: Bearer agent_xxxxx`（独立 agent 令牌，不是浏览器会话）
- 写入、烧录、部署、打卡等一律不开放；agent 只能看，不能改。

令牌有两种拿法：维护者代发（§2），或成员自己用飞书身份换（§3，推荐给个人客户端）。
两者签出的是同一种令牌，scope 与撤销口径完全一致。

门户里还有一页图形版说明：登录后打开 `/mcp-guide`（侧栏「09 MCP 接入」），
把端点、两种拿令牌方式、客户端配置、scope/工具表与常见错误码摆在一起，照着点就能接上；
页面上不请求接口、不显示令牌，令牌明文仍只在签发那一刻出现。

## 1. 为什么是独立令牌

浏览器会话（`dsh_session` cookie）绑的是人的登录状态，给 agent 用会带来三个问题：
无法单独撤销、权限无法收窄、Cookie 语义（SameSite）在 CLI 里不成立。

因此 agent 用**独立凭据**：

| 维度 | 浏览器会话 | agent 令牌 |
| --- | --- | --- |
| 载体 | HttpOnly Cookie | `Authorization: Bearer` 头 |
| 绑定 | 登录用户 | 一个成员 open_id |
| 权限 | 角色（成员/应急） | scope 列表（按数据域收窄） |
| 撤销 | 退出登录/改密 | 单独撤销，即时生效 |
| 落盘 | 会话库 | 只存 sha256 摘要，明文只在创建时返回一次 |

## 2. 签发与撤销（维护者）

维护者 = 应急账号会话，或 open_id 在 `PORTAL_MAINTAINER_IDS` 里（逗号分隔）。
生产环境 `AUTH_EMERGENCY_ENABLED` 默认关闭，所以实务上走后者：

```bash
# /opt/portal/.env
PORTAL_MAINTAINER_IDS=ou_xxxxxxxxxxxx,ou_yyyyyyyyyyyy
```

签发（返回的 `token` 只出现这一次）：

```bash
curl -sS -X POST https://<门户域名>/api/portal/agents \
  -H 'Content-Type: application/json' \
  -b 'dsh_session=<维护者会话>' \
  -d '{"name":"claude-desktop","open_id":"ou_xxxxxxxxxxxx","scopes":["meta.read","tasks.read"]}'
```

列出 / 撤销：

```bash
curl -sS https://<门户域名>/api/portal/agents -b 'dsh_session=<维护者会话>'
curl -sS -X POST https://<门户域名>/api/portal/agents/ag_xxxx/revoke -b 'dsh_session=<维护者会话>'
```

撤销后下一次请求即 401，不需要重启服务。所有签发与撤销都进审计日志
（`agent_create` / `agent_revoke` / `portal_agent_create` / `portal_agent_revoke`）。

## 3. 成员自助签发（设备码授权）

成员不必找维护者：命令行申请设备码 → 浏览器用飞书登录并确认 → 命令行换到令牌。
飞书只用来证明「你是谁」，换出来的仍是本站 agent 令牌（可撤销、按 scope 收窄、
可审计），飞书的 access token 既不落盘也不会发给客户端。

```bash
# 只用标准库，不需要额外依赖
python scripts/mcp_login.py --base-url https://<门户域名>
# 用户码：ABCD-EFGH
# 确认地址：https://<门户域名>/oauth/device?user_code=ABCD-EFGH
# 已保存到 ~/.config/foray-mcp/token.json（权限 600）
```

`--client-name cursor` 改客户端名字（会显示在确认页与令牌列表）；`--no-save`
只打印不落盘；`--out` 换保存路径。

端点与错误码（RFC 8628 风格）：

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| POST | `/oauth/device/authorize` | `device_code`、`user_code`、`verification_uri`、`verification_uri_complete`、`expires_in`(600)、`interval`(5) |
| POST | `/oauth/device/token` | 成功后 `access_token`、`token_type`、`scope`、`agent_id`、`open_id`、`client_name` |
| GET | `/oauth/device` | 确认页（未登录先给飞书入口） |
| POST | `/oauth/device/confirm` | 浏览器提交 `user_code` + `action=approve|deny`，需成员会话 |

轮询期的错误码：`authorization_pending`（还没批准）、`slow_down`（轮询太快，
响应里带新的 `interval`）、`access_denied`（被拒绝）、`expired_token`（码失效或被用过）、
`invalid_request`（缺 `device_code`）。令牌**一次性**：换到手后同一个
`device_code` 再换只会拿到 `expired_token`。

批准时的四道闸门：

1. 必须是**已登录、已开通**的成员会话（未开通 → 403，先去 `/onboarding`）；
2. 会话里的 `tenant_key` 必须在 `FEISHU_ALLOWED_TENANT_KEYS` 里（跨组织 → 403）；
3. 每个成员最多 `MCP_DEVICE_MAX_ACTIVE`（默认 5）个未撤销令牌，超了 → 409，
   先去门户撤销旧的；
4. scope 不由调用方指定：一律 `DEFAULT_SCOPES`（§4 里「默认签发=是」的那七项），
   维护者额外多一个 `sync.read`。

相关配置（`/opt/portal/.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORTAL_MAINTAINER_IDS` | 空 | 维护者 open_id，逗号分隔；同时拥有 §2 的签发权与 `sync.read` |
| `AUTH_DEVICE_FILE` | `config/auth/device_codes.json` | 设备码库（只存 sha256 摘要，权限 600） |
| `MCP_DEVICE_TTL_SEC` | 600 | 设备码有效期 |
| `MCP_DEVICE_INTERVAL_SEC` | 5 | 建议轮询间隔（也用于 `slow_down` 判定） |
| `MCP_DEVICE_MAX_ACTIVE` | 5 | 每人未撤销令牌上限 |
| `MCP_DEVICE_RATE_PER_MIN` | 10 | 申请限频（按来源 IP） |

审计：`agent_device_start`（申请）、`agent_device_grant`（领取成功）、
`agent_create`（批准时建令牌）三条。确认页对同一 IP 连续十条错码会冷却 10 分钟。

## 4. scope 表

scope 按数据域划分，令牌持有哪些就能看见哪些工具（未授权工具在 `tools/list`
里根本不出现，直接调用返回 `-32001`）。

| scope | 内容 | 默认签发 |
| --- | --- | --- |
| `meta.read` | 分组、人员、采购选项 | 是 |
| `projects.read` | 项目与里程碑 | 是 |
| `tasks.read` | 事项列表与详情 | 是 |
| `schedule.read` | 日程 | 是 |
| `procurement.read` | 采购记录 | 是 |
| `quality.read` | 数据质量报告 | 是 |
| `attendance.read` | **本人**考勤状态 | 是 |
| `sync.read` | 同步耗时、队列积压、失败/冲突数 | 否 |

`attendance.read` 只返回令牌绑定成员自己的考勤，不提供他人明细；`sync.read`
属运维视角，默认不给。

## 5. 工具表

| 工具 | scope | 主要参数 |
| --- | --- | --- |
| `portal_meta` | `meta.read` | — |
| `portal_overview` | `meta.read` | — |
| `portal_list_projects` | `projects.read` | `group` `include_archived` `limit` `offset` |
| `portal_get_project` | `projects.read` | `project_id` |
| `portal_list_tasks` | `tasks.read` | `group` `status` `priority` `project_id` `q` `limit` `offset` |
| `portal_get_task` | `tasks.read` | `task_id` |
| `portal_list_schedule` | `schedule.read` | `days` `group` `limit` `offset` |
| `portal_list_procurement` | `procurement.read` | `season` `category1` `unpurchased` `limit` `offset` |
| `portal_get_procurement` | `procurement.read` | `record_id` |
| `portal_quality` | `quality.read` | `limit` `offset` |
| `portal_my_attendance` | `attendance.read` | — |
| `portal_sync_status` | `sync.read` | — |

分页参数统一 `limit`（上限 200）/`offset`；工具返回体是 JSON 文本，外层带
`snapshot_version` / `last_success_at` / `stale`，方便 agent 判断数据新旧。

## 6. 客户端配置

Claude Desktop / Cursor 一类只支持 stdio 的客户端，用 `mcp-remote` 中转：

```json
{
  "mcpServers": {
    "foray-portal": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<门户域名>/mcp",
               "--header", "Authorization: Bearer ${FORAY_AGENT_TOKEN}"]
    }
  }
}
```

支持 streamable HTTP 的客户端直接配 URL + `Authorization` 头即可。

## 7. 协议细节

- `initialize` 返回 `protocolVersion=2025-06-18`、`capabilities.tools`、
  `serverInfo.name=foray-portal-mcp`；`notifications/initialized` 返回 202 无体。
- 只支持单条 JSON-RPC 消息；批量数组返回 `-32600`。
- `GET /mcp` 返回 405（不提供 SSE 流），探活请用 `POST` 的 `ping`。
- 错误码：`-32700` 解析失败 / `-32600` 非法请求 / `-32601` 方法不存在 /
  `-32001` scope 不足 / `-32002` 调用过频 / `-32010` 尚无快照 / `-32603` 服务端异常。
- 限频：每个令牌每分钟 `MCP_RATE_PER_MIN`（默认 120）次 `tools/call`。
- 浏览器来源（带 `Origin` 头）一律 403：MCP 客户端不是浏览器，避免 DNS
  rebinding 借道。

## 8. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 401 `令牌无效或已撤销` | 令牌错/已撤销 | 重新签发，旧令牌不再可用 |
| 403 `浏览器来源不允许` | 客户端带了 Origin 头 | 用 CLI/agent 客户端，别用网页直接调 |
| `-32001` | scope 不足 | 撤销后按需要的 scope 重新签发 |
| `-32010` | 尚无快照 | 等一轮同步完成（看 `/api/portal/sync-status`） |
| `-32002` | 触发限频 | 降低调用频率或调大 `MCP_RATE_PER_MIN` |
| `expired_token` | 设备码超 10 分钟，或已被换过一次 | 重新运行 `scripts/mcp_login.py` |
| `access_denied` | 确认页点了拒绝 | 重新申请，这次点「批准」 |
| 确认页 403「组织不匹配」 | 登录账号不在 `FEISHU_ALLOWED_TENANT_KEYS` | 换本组织账号，或请维护者代发 |
| 确认页 409「令牌数量已达上限」 | 已有 5 个未撤销令牌 | 先在门户撤销不用的令牌 |

审计日志里搜 `mcp_tool_call` 可以看到每次调用的 agent、工具、耗时与失败原因
（不含令牌本身）。
