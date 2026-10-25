# D2 身份与账号设计（阶段 9 执行依据）

> 定稿：2026-09-10。依据维护者口述要求（见 `docs/team-portal-migration-plan.md` 阶段 9 的"已确认口径"）。
> 与 D1 的关系：D1（阶段 3）只有"飞书会话 + 手工应急账号"；D2 引入**本地账号**作为离线调试凭据，
> 并**取消应急账号**。

## 1. 角色与真相源

| 概念 | 说明 |
| --- | --- |
| 成员真相源 | **飞书**：`tenant_key` 白名单 + 通讯录 status 复核（现有 `auth_guard.recheck_loop`） |
| 本地账号 | 绑定 `open_id` 的凭据记录（用户名 + PBKDF2 密码散列），**由飞书首次登录开通**，不提供任何公开注册 |
| 会话 | 沿用 `config/auth/sessions.json`；新增 `auth_method: feishu|local` 与 `account_id` 字段 |
| 审计 | `config/auth/audit.log`（JSONL 追加）：开通 / 登录成功 / 登录失败（含限频触发）/ 改密 / 重置 / **删除** |

## 2. 数据模型（`config/auth/accounts.json`，600，原子写）

```json
{
  "version": 1,
  "accounts": {
    "ou_xxxxxxxx": {
      "username": "zhangsan",
      "display": "张三",
      "tenant_key": "<tenant_key>",
      "pbkdf2": {"algo": "pbkdf2_sha256", "iter": 200000, "salt": "<b64>", "hash": "<b64>"},
      "status": "active",
      "created_at": 1789036800.0,
      "password_updated_at": 1789036800.0,
      "last_login_at": 1789036900.0,
      "last_seen_org_at": 1789036900.0
    }
  }
}
```

- 用户名唯一（大小写不敏感、去空白）；3–32 字符，允许中文/字母/数字/下划线/连字符。
- **明文密码永不落盘**；写文件用"临时文件 + `os.replace`"原子替换，权限 600。
- 反向索引在内存构建（username → open_id），进程启动/写入后重建。
- `config/auth/` 已在 `portal_backup.py` 的备份范围内；账号库随备份走，恢复即生效。

## 3. 开通（首次飞书登录）

1. 飞书 OAuth 回调成功 → 建立**待开通会话**（`account_id` 存在但 `password_set=false`）。
2. guard 对该会话：**除开通页与其接口、静态资源、登出外，任何页面路径/管理接口都 302/403 到 `/onboarding`**。
3. `POST /api/auth/account/onboard {username, password, password2}`：
   - 校验用户名唯一与格式、密码长度 ≥8 且不等于用户名、两次一致；
   - 写账号（PBKDF2）、写审计、把会话标记为已开通 → 进入正常成员权限。
4. **没有注册端点**：非飞书会话无法创建账号；`/api/auth/account/register` 不存在（用例断言 404）。

## 4. 登录路径与权限矩阵

| 场景 | 会话来源 | 复核状态 | 门户页面/管理 API | 调试页面/API/WS |
| --- | --- | --- | --- | --- |
| 飞书登录（已开通） | feishu | ok | ✅ | ✅ |
| 本站账号登录（联网） | local | ok | ✅ | ✅ |
| 本站账号登录（断网/上游不可用） | local | recheck_unavailable | ❌ 403 `recheck_unavailable` | ✅ |
| 飞书登录（断网） | feishu | recheck_unavailable | ❌ 同上 | ✅ |
| 账号被删除/停用，或成员已退出组织 | 任意 | revoked/expired | 401 | 401（WS 握手 4401；连接期撤销 4403） |

- **断网登录可用**：本地登录只做本地 PBKDF2 校验，不依赖飞书；离线非常态、不设时长配额（维护者口径 2/4）。
- 权限判定**继续复用 D1 的 guard 状态机**，不新增状态；D2 只增加"凭据来源"。

## 5. 成员退出组织 → 删除本地账号（不可逆）

触发点（任一）：
1. 复核循环返回 `resigned/exited/not_found`（现有 `member_status` 判定）；
2. 会话校验时联系人接口明确返回"用户不在可见范围/已离职"。

处置：**删除账号记录** → 撤销该 `open_id` 的全部会话（含已建立 WS 4403） → 审计写 `account_deleted{open_id, username, reason}`。
重新加入组织需**再次飞书登录重新开通**（设置新的本站密码）。

**已知限制（必须在文档与用例中如实标注）**：断网期间无法判定成员是否已退出组织，此时其本地账号仍可登录**调试**
（不能看门户）。这是维护者口径 2（断网允许调试）与口径 3（退出即删除）之间不可避免的取舍。

## 6. 忘记密码

| 场景 | 流程 |
| --- | --- |
| 联网 | 登录页"忘记密码" → 飞书再验证（同 OAuth 流程，但标记 `purpose=reset`）→ 校验 open_id 有账号 → 设置新密码 → 审计 `password_reset{by=feishu}` → 撤销该账号旧会话（可选，默认撤销） |
| 断网 | 维护者在服务器本地执行 `scripts/account_ctl.py reset-password <username>`（交互输入新密码）→ 审计 `password_reset{by=maintainer}` |

`account_ctl.py` 子命令：`list` / `disable <username>` / `enable <username>` / `reset-password <username>` / `delete <username>`。

## 7. 应急账号退役

- 删除 `/api/auth/emergency/login` 路由与登录页应急表单；`scripts/emergency_ctl.py` 标记退役（保留文件与一段迁移说明，避免历史文档断链）。
- 现有 `config/auth/emergency_users.json`（生产为空，已实测）不再被读取；如存在则仅保留文件不加载。
- 破坏性兜底只剩"维护者 SSH 上用 `account_ctl.py reset-password`"——这是维护者明确接受的口径 5。

## 8. HTTPS（与账号体系同批）

- **证书**：自建本地 CA + 服务器证书（SAN：`portal.example.com`、`192.168.1.130`），有效期 2 年；私钥 600 不入库；
  签发脚本入库（可重放），续期步骤写运维手册。
- **服务**：uvicorn 直接 TLS（`--ssl-certfile/--ssl-keyfile`，单 worker），80 端口保留一个极简 301 → https。
- **回调**：飞书登记追加 `https://portal.example.com/auth/feishu/callback`（只增不删）；`.env` 的 `FEISHU_REDIRECT_URL` 同步改 https。
- **Cookie/同源**：`SESSION_COOKIE` 加 `Secure`；`_check_same_origin` 接受 https 源；WS 用 `wss`。
- **客户端**：`wsctl.py` 支持 `--cafile`（自签 CA）并默认校验；`portal_health.py` 支持 https + CA；
  成员浏览器需安装一次 CA（或由维护者分发），未安装会看到证书警告——**这不是故障**，写入手册与交接说明。
- **需要维护者配合**：在端口转发侧新增 **443 → 130:443**（现场实测 443/8443 均被拒，80/8000/<SSH端口> 可连）。

## 9. 需要改动的代码面（供实施参考）

| 位置 | 改动 |
| --- | --- |
| `app/auth/accounts.py`（新） | 账号库读写、用户名索引、PBKDF2、审计日志 |
| `app/auth/local_login.py`（新） | 本地登录校验 + 限频（复用 D1 的失败锁定参数） |
| `app/auth/guard.py` | 新增 `onboarding` 状态与路由白名单；应急路径移除；https 同源 |
| `app/main.py` | 开通/登录/找回/重置路由；应急登录路由删除；异常处理保持 |
| `app/auth/feishu.py` | 回调后判定"是否已开通"；`purpose=reset` 分支 |
| `app/auth/sessions.py` | 会话增加 `auth_method`/`account_id`；按账号批量撤销 |
| `static/`（阶段 10） | 登录/开通/找回三页 + 统一资源 |
| `scripts/account_ctl.py`（新） | 维护者本地账号管理；`emergency_ctl.py` 退役 |
| `scripts/tls_issue.py`（新） | 本地 CA 与服务器证书签发/续期 |
| `deploy/portal.service` | TLS 启动参数；80→443 跳转单元 |
| 测试 | `test_auth.py` 扩展开通/本地登录/删除/重置；新增 HTTPS 用例；`test_acceptance.py` 的应急用例改写 |

## 10. 验收（对阶段 9 清单的映射）

9-1→§2/§9；9-2→§3；9-3→§4；9-4→§5；9-5→§6；9-6→§7；9-7/9-8→§8；9-9→回归+部署+回滚。
所有"删除/重置"路径必须有审计记录与用例，**不得只在文档里声明**。
