# 部署包、目录与发布检查（阶段 7）

> 目的：把"代码在仓库里长什么样"与"服务器上实际跑什么"对齐成一份可审阅清单，
> 并给出每次发布前必须跑的检查。运维操作步骤见 `docs/portal-operations.md`。

## 1. 仓库 → 服务器映射

| 仓库路径 | 服务器路径 | 是否随部署覆盖 | 说明 |
| --- | --- | --- | --- |
| `app/`、`static/`、`wsctl.py`、`run.sh`、`requirements.txt` | `/opt/portal/` 同名 | ✅ 覆盖 | 应用代码与前端 |
| `deploy/portal.service` | `/etc/systemd/system/portal.service` | ✅ 覆盖 | 单 worker、`--port 80` |
| `scripts/`（含 `emergency_ctl.py`、`portal_backup.py`、`portal_restore.py`） | `/opt/portal/scripts/` | ✅ 覆盖 | 运维脚本 |
| `docs/`、`README.md` | `/opt/portal/` | ✅ 覆盖 | 随包带走，便于服务器上排障 |
| `config/`（飞书配置/设备注册表/预设/`auth/`/快照库） | `/opt/portal/config/` | ❌ **永不覆盖** | 打包硬校验拦截；升级前另存到 `/opt/portal-backups/<ts>/` |
| `config/auth/accounts.json` + `audit.log`（D2 账号库与审计） | 同左 | ❌ **永不覆盖** | 随 `config/` 一起备份/恢复；权限 600 |
| `tls/`（本地 CA 与服务器证书，阶段 9） | `/opt/portal/tls/` | ❌ 不部署 | 由 `scripts/tls_issue.py` 在服务器本地签发；目录 700、私钥 600 |
| `firmware/` | `/opt/portal/firmware/` | ❌ **永不覆盖** | 同上（首装时从旧目录 `cp -an` 补种） |
| `.env` | `/opt/portal/.env` | ❌ **永不覆盖** | 600；样例 `deploy/portal.env.example` |
| `tests/`、`backups/`、`参考/`、`tests/e2e/browser-libs/` | — | ❌ 不部署 | 打包排除（体积与噪音） |
| `deploy.py` | — | ❌ 不部署 | 从开发机执行 |

对照旧部署方式（阶段 7 前的 `deploy.py`）：整包 322 文件 / 18.5 MB（含 `backups/`、`参考/`、
`config/feishu.json`），`cp -a` 覆盖 `/opt/stm32-webserver` 后 `systemctl restart webserver`
——既可能覆盖线上身份库/快照库，又必然触碰 80 端口生产服务。**已废弃**。

## 2. 服务器现状（2026-09-10 切换后）

| 项 | 值 |
| --- | --- |
| 主机 | `<服务器主机名>`，`192.168.1.130`（域名 `portal.example.com` 端口转发：80/8000/SSH <SSH端口>） |
| 生产门户 | `/opt/portal`，`portal.service`（active，`User=root`，`--port 80`，单 worker） |
| 回滚目标 | `/opt/stm32-webserver`，`webserver.service`（inactive，文件未动） |
| 预发实例 | `~/portal-test`（手工进程，`--port 8000`，复用旧 venv，仍在线） |
| 数据备份根 | `/opt/portal-backups/`（每次升级前 `config/`+`firmware/` 快照） |
| OpenOCD | USB：`/usr/bin/openocd` 0.11（与旧调试台一致）；TCP-DAP：`/opt/openocd-vllink/bin/openocd` 0.12+（`OPENOCD_TCP_BIN` 默认值） |
| Python | 系统 3.10.12；`/opt/portal/.venv`（部署时创建并装依赖，运行期不联网） |

## 3. 环境变量样例

完整键名与默认值见 `deploy/portal.env.example`（与 `app/config.py` 一一对应），
线上形态为 `/opt/portal/.env`（600）。关键几项：

```ini
WEB_PORT=80
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=<只放这里>
FEISHU_REDIRECT_URL=http://portal.example.com/auth/feishu/callback
FEISHU_ALLOWED_TENANT_KEYS=<tenant_key>
PORTAL_SYNC_INTERVAL=60
OPENOCD_TCP_BIN=/opt/openocd-vllink/bin/openocd
SCAN_SUBNET=192.168.1
```

## 4. 发布检查清单（每次发布照抄）

1. **回归全绿**：`.venv/bin/python tests/e2e/run_all.py`（0 失败；SKIP 项单列，不计入通过）。
2. **打包自检**：`.venv/bin/python deploy.py --dry-run`
   —— 输出"受保护路径校验：通过（0 条）"与"单 worker 校验：通过"，包含完整中文字库后，包体约 10 MB；字体按需分包加载。
3. **备份**：部署脚本会自动做；手册操作前另可手工跑
   `scripts/portal_backup.py --out /opt/portal-backups/manual-<ts> --tar`。
4. **部署**：`deploy.py --host <地址> --ssh-port <端口>`（首装加 `--seed-data … --cutover`）。
5. **探针**（脚本自动跑，失败会自动回滚）：`/login` 200、`/portal/portal.css` 200、
   `/api/portal/overview` 401、`/portal/fixtures.js` 404。
6. **同步确认**：`journalctl -u portal.service -n 20 | grep 门户同步完成`；
   `counts` 与实际表数一致、`last_error` 空。
7. **回滚可用性**：确认 `webserver.service` 单元文件仍在（`deploy.py --rollback` 随时可用）。
8. **记录**：把本轮日志路径写进 `docs/portal-progress.md`（含改动文件与回退方式）。

## 5. 切换与回滚（已演练）

```bash
# 切换（停旧服务、起门户；旧目录保留）
python deploy.py --password <口令> --host portal.example.com --ssh-port <SSH端口> \
    --target portal --seed-data ~/portal-test --cutover
# 实测：portal.service active；外部 http://portal.example.com/ → 302 /login?next=/；/api/status → 401

# 回滚（停门户、起旧调试台）
python deploy.py --password <口令> --host portal.example.com --ssh-port <SSH端口> --rollback
# 实测：webserver.service active；外部 / → 200（旧调试台）、/login → 404；/opt/portal 数据保留
```

## 6. 不随包交付的东西（需人工维护）

- 飞书开放平台侧：应用权限、可用范围、**重定向 URL 登记**、版本发布审批。
- 服务器侧的 `/opt/portal/.env` 真实密钥；`config/auth/` 账号与会话。
- 硬件相关：`/etc/udev/rules.d/99-vllogic.rules`（仓库有副本）、OpenOCD fork 构建 `/opt/openocd-vllink`、
  `/opt/stm32-webserver/firmware` 里的历史固件。
