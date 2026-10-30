# 门户运维手册（阶段 3 起草 → 阶段 7 完稿）

> 状态：**阶段 9（D2 身份与账号）已上线（2026-09-11）**：登录改为"本站用户名+密码 + 飞书开通"，
> 无应急账号、无公开注册；HTTPS 工具链就绪（等端口转发 443 → 130:443 后一条命令启用）。
> 历史状态：阶段 7 完稿（2026-09-10）。生产门户已上线：`http://portal.example.com/`（端口转发到
> 实验室服务器 `192.168.1.130` 的 **443 端口**（80 做 301 跳转）。**校园网内维护入口：`ssh -p <SSH端口> <用户>@portal.example.com`**。旧调试台代码与数据
> 保留在 `/opt/stm32-webserver`，作为一键回滚目标（`webserver.service`，当前 inactive）。
> 本文件覆盖：上线形态（§A）、部署与升级（§B）、备份与恢复（§C）、凭据轮换（§D）、
> 监控与巡检（§E）、应急账号（§1）、会话与数据文件（§2）、飞书配置（§3）、访问模型（§4）、
> CLI（§5）、回退方式（§6）、常见故障（§7）、同步与快照（§8）、未完成项（§9）。

## A. 上线形态（2026-09-10 切换后实测）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 门户地址 | **校园网内**：`https://portal.example.com/`（→ 130:443，TLS）；`http://` 由 80 端口 301 跳转 | 外部实测 https `/login` 200、`/` 302、匿名 API 401、`http://…/tasks` 301 保留路径；不装 CA 则证书校验失败（真实 TLS） |
| 调试台 | `http://portal.example.com/debug/`（需登录） | 匿名 302 `/login?next=/debug/` |
| 应用目录 | `/opt/portal`（代码 + `config/` + `firmware/` + `.venv`） | 见 §B |
| systemd 单元 | `portal.service`（`User=root`，`--port 80`，**单 worker**） | `deploy/portal.service` |
| 环境变量 | `/opt/portal/.env`（600；`FEISHU_*` 等） | 样例 `deploy/portal.env.example` |
| 预发/测试实例 | `portal.example.com:8000`（`~/portal-test`，手工进程，**仍在跑**） | `pgrep -af 'port 8000'` |
| 回滚目标 | `/opt/stm32-webserver` + `webserver.service`（inactive，文件未动） | §B.4 回滚演练已通过 |
| 数据备份根 | `/opt/portal-backups/<时间戳>/`（每次升级前自动） | §B.3 |
| 统一设计资源 | `/opt/portal/static/ui/{tokens.css,base.css}`（门户与调试台共用；`/ui/` 资源匿名可读、HTML 受鉴权） | 阶段 10 v1，见 `static/ui/README.md` |
| 飞书回调登记 | `http://portal.example.com/auth/feishu/callback`（另保留 130:80 与 130:8000 两条） | 官方 `PATCH .../config` code=0；授权页实测 302 到 passport 登录页 |

`portal.service` 目前与旧调试台一致以 `root` 运行（要写 `config/`、调用 OpenOCD/串口；udev
已把 `ttyACM*`/`hidraw*` 放开为 0666）。降权运行的等价单元见 `deploy/portal.service` 注释。

## B. 部署与升级

> 2026-10-25 起以 `接续开发说明.md` §4 为准：`deploy.py` 默认读 `config/deploy_server.json`
> （不用再传 `--password/--host`），退回用 `--list-backups` + `--restore <时间戳>`；
> 旧调试台已不存在，下文 `--rollback` 只作历史记录。`deploy.py` 于当日按本手册与现场重建，
> 见 `docs/rescue-20261025.md`。

### B.1 一次性安装（已完成，留作重建步骤）

```bash
# 1) 目标机装依赖（运行期不需要联网）：python3-venv + 项目 venv
sudo apt-get install -y python3-venv
# 2) 从开发机执行部署（默认目标 portal，端口 80）
python deploy.py --password <口令> --host <地址> --ssh-port <端口> \
    --target portal --seed-data <既有数据目录，如 ~/portal-test> --cutover
# 3) 自检（不连服务器）
python deploy.py --dry-run
```

### B.2 `deploy.py` 的保护与校验（阶段 7 重写）

- **打包排除持久数据**：`config/`、`firmware/`、`backups/`、`参考/`、`tests/e2e/browser-libs/`、
  `.env`、`*.db`、`*.log` 一律不进包；打包后**硬校验**，出现受保护路径即中止。
- **代码与数据分离**：门户装到 `/opt/portal`；旧目录 `/opt/stm32-webserver` 不动，因此
  "停门户、起旧服务"就是真回滚。
- **升级前自动备份**：`/opt/portal/{config,firmware}` → `/opt/portal-backups/<时间戳>/`。
- **不偷偷抢端口**：80 被 `webserver.service` 占用且未给 `--cutover` → 直接中止。
- **单 worker 校验**：`ExecStart` 出现 `--workers` 即中止（同步是进程内单例调度）。
- **快照库迁移（2026-10-21 起）**：重启前执行 `scripts/portal_migrate.py`，先把库备份到
  `/opt/portal-backups/<时间戳>/premigrate/` 再迁移；已是新版本或首次安装无库时直接跳过。
  迁移失败即中止、不重启，旧进程照常运行。说明见 §8.1。
- **失败即停**：`sudo` 预检 + 关键步骤 fail-fast；验证探针不过 → **自动回滚**到旧服务。
- 已知约束：`--target legacy` 仍保留（覆盖旧目录），仅历史兼容用，不要再用。

### B.3 日常升级流程

```bash
# 1) 本地全量回归全绿（门槛见 README，当前 375 项 + 阶段 7 新增项）
.venv/bin/python tests/e2e/run_all.py
# 2) 部署（会先备份数据，再覆盖代码、迁移快照库、重启单元、跑四个探针）
python deploy.py --password <口令> --host <地址> --ssh-port <端口>
# 3) 人工确认
curl -s -o /dev/null -w '%{http_code}\n' http://<地址>/login          # 200
curl -s -o /dev/null -w '%{http_code}\n' http://<地址>/api/status     # 401（匿名）
```

### B.4 回滚（已演练，2026-09-10 实测通过）

```bash
python deploy.py --password <口令> --host <地址> --ssh-port <端口> --rollback
# 实测：portal.service → inactive，webserver.service → active，
#       80 端口回到旧调试台（/ → 200、/login → 404、/api/status → 200 匿名）
#       /opt/portal 数据与 config/ 保留（不删除），随时可再切回
```

## C. 备份与恢复（阶段 7 新增，隔离演练已通过）

### C.1 备份

```bash
cd /opt/portal
/opt/portal/.venv/bin/python scripts/portal_backup.py \
    --data-dir /opt/portal --out /opt/portal-backups/manual-$(date +%Y%m%d-%H%M) \
    --label 手工备份 --tar
```

产物：`files/`（逐文件副本，含 `config/`、`firmware/`、`.env`）、`portal_snapshot.db`
（**sqlite3 backup API 的一致性副本**，WAL 下也含最新已提交事务）、`manifest.json`
（逐文件 sha256 + 大小 + schema 状态 + 快照 meta）、可选 `*.tar.gz`。

- **覆盖范围**：飞书配置、设备注册表、预设与设备预设、`config/auth/`（会话 + 应急账号）、
  快照库、固件、`.env`。
- 库版本 2 起，`manifest.snapshot` 多出 `outbox_open`（还没推到飞书的网页改动条数）与
  `conflicts_open`（未确认冲突条数），输出里也会打印。这些改动只存在快照库里，恢复时库文件必须一起恢复。
- 快照库版本与程序不一致时**照样备份**（数据仍要救），但 `manifest.schema.ok=false`
  且退出码 2，并提示"恢复后先迁移或换匹配版本"。
- 不存在 `sqlite3` 命令行也能用（130 上就没装）：脚本走 Python `sqlite3` 模块。

### C.2 恢复

```bash
/opt/portal/.venv/bin/python scripts/portal_restore.py \
    --bundle /opt/portal-backups/<时间戳> --target /opt/portal --dry-run   # 只校验
/opt/portal/.venv/bin/python scripts/portal_restore.py \
    --bundle /opt/portal-backups/<时间戳>.tar.gz --target /tmp/restore-drill # 先恢复到临时目录验证
```

- 恢复前**逐文件校验 sha256**，任一不一致即中止（不静默写入坏数据）。
- 目标目录非空且未 `--force` → 拒绝（避免误覆盖在用数据）；恢复时清掉不配套的
  `-wal`/`-shm`。
- 演练结论（`tests/e2e/test_backup_restore.py`，29 项全绿）：篡改包必被发现、tar.gz 形态可恢复、
  普通文件字节一致、库 `integrity_check=ok` 且 `current_version` 一致、非空目标需 `--force`、
  未知版本库备份/恢复都给出"先迁移"提示。

## D. 凭据轮换（验收要求：轮换不丢业务配置）

| 对象 | 操作 | 影响面（已实测） |
| --- | --- | --- |
| 应急账号 | `emergency_ctl.py create <名>`（轮换）/ `disable` / `enable` | 只动 `config/auth/emergency_users.json`；旧会话全部废止 |
| 飞书 App Secret | 开放平台重置 → 改 `/opt/portal/.env` 的 `FEISHU_APP_SECRET` → `systemctl restart portal` | **只动 `.env`**：演练证明 `config/` 下业务文件哈希逐个不变（`test_backup_restore.py` 第 6 组） |
| tenant_key 白名单 | 改 `.env` `FEISHU_ALLOWED_TENANT_KEYS` → 重启 | 只影响登录放行集合；不动数据 |

轮换后自检：`GET /api/portal/sync-status`（成员）`last_error` 为空、`counts` 正常。

## E. 监控与巡检

```bash
systemctl status portal.service                 # 进程与最近日志
journalctl -u portal.service -n 50 --no-pager   # 同步轮/错误类别
curl -s http://127.0.0.1/api/portal/sync-status -H "Authorization: Bearer <成员令牌>"
```

| 观察点 | 正常 | 异常处置 |
| --- | --- | --- |
| 同步间隔 | 每 ~75s 一轮（60s 间隔 + ~13s 轮耗时） | 见 §7 / §8 |
| `last_error` | 空 | 类别含义：`network` 上游不可达/超时、`rate_limited` 限流、`api` 飞书报错、`config` 配置/字段映射、`parse` 数据形状、`schema` **快照库版本不匹配（见 §8.1）**、`internal:*` 内部异常 |
| `stale` | false | true = 超 `PORTAL_SYNC_STALE`（默认 180s）未成功 |
| 磁盘 | `/` 有空间 | 快照库只保留当前版本行（发布时清理旧版） |

**已落地的自动巡检（阶段 8，见 §F）**：`portal-health.timer` 每 5 分钟跑一次
`scripts/portal_health.py`（同机直读快照库 meta，不需要令牌），退出码即告警契约：
`0` 正常 / `3` 服务或库不可达 / `4` schema 版本不匹配 / `5` 同步连续失败 / `6` 超时未同步 / `7` 令牌无效（HTTP 模式）/
`8` 本地写入队列异常（有推送失败的操作，或未推送改动积压超过 `--queue-age`，默认 900s；见 §8.2）。
失败会以非零退出记进 journald（`journalctl -u portal-health.service`），接入 webhook/邮件只需再包一层。

人工告警阈值建议：`sync-status.last_error` 连续 3 轮非空、或 `stale=true` 持续 10 分钟。

## F. 定时备份与健康巡检（阶段 8 落地）

| 单元 | 触发 | 作用 |
| --- | --- | --- |
| `portal-backup.timer` → `portal-backup.service` | 每日 03:30（`Persistent=true`，错过开机补跑） | 跑 `scripts/portal_backup_daily.sh`：备份 `config/`+`firmware/`+`.env`+快照库一致性副本，`--keep 14` 保留最近 14 份，并出 `daily-*.tar.gz` |
| `portal-health.timer` → `portal-health.service` | 每 5 分钟 | 跑 `scripts/portal_health.py --db /opt/portal/config/portal_snapshot.db`，非零退出即"要人看" |

- 实测（2026-09-10 部署后）：`portal-health.service` → `ExecMainStatus=0`，日志
  `OK 门户健康（直读 …）：同步正常，version=57 counts={"projects": 2, …}`；
  `portal-backup.service` → `ExecMainStatus=0`，产出 `/opt/portal-backups/daily-20260910-200124/` + `.tar.gz`（2.0 MB）。
- 保留策略只删"含 `manifest.json` 的备份目录"，**永不触碰** `config/`、`firmware/`、`.env` 本体
  （用例 `tests/e2e/test_ops.py::t_backup_keep` 专门验证：无关目录不删、数据本体不动）。
- 手动运行：
  ```bash
  sudo systemctl start portal-backup.service && journalctl -u portal-backup.service -n 20
  sudo systemctl start portal-health.service  && journalctl -u portal-health.service -n 20
  ```
- **恢复抽检（建议每月一次）**：把最近一份 `daily-*.tar.gz` 恢复到临时目录做 dry-run 与真实恢复，
  核对 `integrity_check`、`current_version`、`counts`：
  ```bash
  /opt/portal/.venv/bin/python scripts/portal_restore.py \
      --bundle /opt/portal-backups/daily-<时间戳>.tar.gz --target /tmp/restore-drill --force
  ```
- 备份目录属主为 root（服务以 root 运行），普通用户 `ls` 会看到空目录——用 `sudo ls` 查看。

## G. 存储维护（阶段 8）

- **快照库 WAL**：发布成功且 WAL 超过 4 MB 时自动 `PRAGMA wal_checkpoint(TRUNCATE)`
  （`app/portal/store.py::maybe_checkpoint`）。实测生产 WAL 由 1.8–4 MB 降到 **78 KB**。
- **journald 上限**：`/etc/systemd/journald.conf.d/portal.conf`（随部署安装）
  `SystemMaxUse=300M`、`SystemKeepFree=1G`、`MaxRetentionSec=30day`；部署时 `systemctl restart systemd-journald` 生效
  （安装前实测 128 MB 且无上限）。
- **快照库自身**：只保留当前版本行（发布时清理旧版本）；`schema_version` 由 §8.1 的闸门保护。

## H. 旧调试台退役（条件与步骤，阶段 8 只定计划）

现状：`/opt/stm32-webserver` + `webserver.service`（inactive）保留为**一键回滚目标**。

退役条件（全部满足再动）：①门户在 80 端口稳定运行 ≥ 4 周且无回滚需求；
②近 4 周的每日备份齐全且抽检恢复通过；③维护者确认不再需要旧界面；
④确认没有其它脚本/开机项依赖旧目录（`systemctl show webserver.service -p ExecStart`、`crontab -l`）。

退役步骤（可逆）：①`sudo systemctl disable webserver.service`（已 disable）；
②`sudo tar -czf /opt/portal-backups/legacy-retire-$(date +%F).tar.gz -C /opt stm32-webserver`；
③`sudo mv /opt/stm32-webserver /opt/stm32-webserver.retired-$(date +%F)`；
④保留 `/etc/systemd/system/webserver.service` 文件但确认 unit 指向已改（或一并移走）；
⑤更新本文档 §A 与 `docs/portal-deploy.md` §2。**本阶段不执行**，仅备查。

## I. 本站账号体系（D2，阶段 9 上线）

| 项 | 说明 |
| --- | --- |
| 账号库 | `config/auth/accounts.json`（JSON、600、临时文件 + `os.replace` 原子写）；只存 PBKDF2 散列，**无明文密码** |
| 审计 | `config/auth/audit.log`（JSONL，5 MB 轮转）：`account_onboard` / `login_ok` / `login_failed` / `password_reset` / `account_deleted` / `feishu_login` / `account_disabled`；**不含密码与散列** |
| 开通 | 只有**组织成员经飞书登录**才能开通（首次登录 → `/onboarding` 设置用户名+密码）；**无公开注册端点** |
| 登录 | 门户登录页 = 本站用户名+密码 + "飞书登录"按钮；`POST /api/auth/local/login`（失败限频：`AUTH_LOCAL_MAX_FAIL`/`LOCK_SEC`） |
| 权限（联网） | 会话有效 + 复核 ok → 门户 + 调试台 |
| 权限（断网） | 复核不可用（`recheck_unavailable`）→ **仅调试**：管理页 302 `/login?recheck=1`、管理 API 403；调试页/API/**WS 全部可用**（烧录、串口、遥测不受影响） |
| 退出组织 | 复核确认 `resigned/exited` → **删除本地账号 + 撤销该 open_id 全部会话（含 WS 4403）+ 审计 `account_deleted{reason:left_org}`**；**不可逆**，重新加入需再走飞书重新开通 |
| 忘记密码（联网） | 登录页"忘记密码" → `GET /auth/feishu/start?purpose=reset` → 飞书再验证 → **10 分钟一次性改密窗口** → `/reset` 页设置新密码（旧密码与旧会话同时失效） |
| 忘记密码（断网） | 维护者在服务器本地重置（见下） |
| 应急账号 | **已停用**（`AUTH_EMERGENCY_ENABLED` 默认 0）：`POST /api/auth/emergency/login` 返回 404 明确提示；登录页无应急表单；`scripts/emergency_ctl.py` 退役（保留文件供历史文档引用） |
| 会话 | `config/auth/sessions.json` 增 `auth_method`(feishu\|local)、`account_id`、`onboarded`；成员 12h（`AUTH_SESSION_MEMBER_TTL`） |

**维护者本地操作（断网兜底）**：

```bash
cd /opt/portal
.venv/bin/python scripts/account_ctl.py list                     # 账号清单
.venv/bin/python scripts/account_ctl.py reset-password <用户名>  # 交互设置新密码（并撤销旧会话）
.venv/bin/python scripts/account_ctl.py disable <用户名>         # 停用 + 撤销会话
.venv/bin/python scripts/account_ctl.py delete <用户名> --yes    # 删除（不可逆，写审计）
```

**已知限制（如实）**：断网期间无法判定成员是否已退出组织 —— 此时其本地账号仍可登录**调试**（看不到门户），
直到下一次成功复核才会触发删除。这是"断网仅调试"与"离职即删除"两条口径之间不可避免的窗口。

## J. HTTPS（自签内网 CA，阶段 9 就绪）

### J.1 开通 443 转发的三种方式（路由器侧）

站点 `portal.example.com:443` 能连上的前提是**转发侧把 443 指到 `192.168.1.130:443`**。三种做法，按省事程度排：

1. **`.bat` 一键脚本（本仓库自带，适合 Windows）**：`scripts/enable_443_forward.bat`
   ```bat
   scripts\enable_443_forward.bat -check     rem 只读检查：列出既有转发、看是否已有 443（先跑这个）
   scripts\enable_443_forward.bat            rem 新增 443 -> 192.168.1.130:443
   scripts\enable_443_forward.bat -remove    rem 撤销
   ```
   - 原理：用 Windows 自带 `ssh`/`scp` 连到**默认网关**（即当前所连路由器），在路由器上跑 UCI：
     **从现有 80 转发规则克隆** WAN 区域与目标 IP，只新增一条名为 `portal443` 的 DNAT 规则，
     `uci commit firewall` + `/etc/init.d/firewall reload`。全程**不改动任何既有规则**，且幂等。
   - **v2（2026-09-11 修订，已按真机报错修正）**：不再依赖 `scp`（有些 Windows 只装了 `ssh.exe`，
     会报 `'scp' 不是内部或外部命令`）——远端脚本改为**单个 `ssh` 参数**传入，**只需输入一次 root 密码**；
     批处理内消息改为**纯 ASCII**（中文在 GBK 控制台会乱码）；首次连接用
     `-o StrictHostKeyChecking=no -o UserKnownHostsFile=%TEMP%\portal443_known_hosts`（不污染本机 known_hosts）。
   - **v3（2026-09-11 第三次修订，真机报错驱动）**：真机上出现
     `router (default gateway):` 为空 → `ssh root@` 连空主机 → `banner exchange: Connection to UNKNOWN port -1:
     Connection refused`（exit 255）。原因是网关探测拿到**空白/不可用值**而 v2 的空值判断没兜住（我方缺陷）。
     v3 改为：**内置默认网关 `192.168.1.1`（维护者 2026-09-11 确认，ping 通即直接用）→ `-gw <ip>` 覆盖
     → `route print -4` 解析（语言无关，不依赖 PowerShell）→ PowerShell 兜底**，
     之后做**严格 IPv4 校验 + ping 预检**，不合法就带诊断信息退出；支持 `ROUTER_IP` 环境变量；文件保持纯 ASCII。
     因此现在**双击即可**（无参数），先跑 `-check` 更稳。
   - 排错时发现并修掉的两个真问题：① 批处理里**字面量 `^` 必须写成 `^^`**（否则 `sed 's/^firewall…[^.]…'`
     被 cmd 吃掉字符 → 误报"找不到 80 模板"）；② CHECK 载荷里 `echo … (count)` 的圆括号会被远端 `sh`
     当成子 shell（`Syntax error: "(" unexpected`）。
   - 找不到 80 模板或模板无 `dest_ip` 时会**明确报错并停止**（说明转发可能在上游设备，不瞎写）。
   - 远端逻辑已用**模拟 uci** 在本地验证过（含 `src_dport='80 8000'` 这类多端口模板、重复执行幂等）；
     但没有 X-WRT 真机验证过，所以**第一次请先跑 `-check`**。
2. **LuCI 图形界面（最稳，不依赖 SSH）**：登录路由器 → 网络 → 防火墙 → 端口转发 → 添加：
   名称 `portal-https`、协议 `TCP`、外部区域 `wan`、外部端口 `443`、内部区域 `lan`、
   内部 IP `192.168.1.130`、内部端口 `443` → 保存并应用。（照抄现有 80 那条的字段最保险。）
3. **SSH 手工（等价于脚本所做的事）**：
   ```sh
   uci set firewall.portal443=redirect
   uci set firewall.portal443.name='portal-https'
   uci set firewall.portal443.target='DNAT'
   uci set firewall.portal443.src='wan'        # 照抄现有 80 规则的 src
   uci set firewall.portal443.src_dport='443'
   uci set firewall.portal443.dest='lan'
   uci set firewall.portal443.dest_ip='192.168.1.130'
   uci set firewall.portal443.dest_port='443'
   uci set firewall.portal443.proto='tcp'
   uci commit firewall && /etc/init.d/firewall reload
   ```

**注意**：转发只解决"能连到"；服务端还要切 HTTPS（`portal_tls.py enable`，见下文）。
很多家用/校园路由器不支持**回环 NAT**，因此在局域网内访问 `https://portal.example.com/` 可能仍失败——
真实判断请到局域网外用手机流量测试。

**为什么自签**：`portal.example.com` 解析到内网地址 `10.0.0.1`，公网无法回连 → Let's Encrypt HTTP-01 不可用；
维护者选定本地 CA。**前提**：端口转发需打开 **443 → 130:443**（当前仅 80/8000/<SSH端口>）。

```bash
# 一次性启用（在 /opt/portal，需 sudo）
sudo .venv/bin/python scripts/portal_tls.py status     # 看当前形态
sudo .venv/bin/python scripts/portal_tls.py enable            # 标准：门户 443 + 80 做 301
sudo .venv/bin/python scripts/portal_tls.py enable --port 80  # 过渡：443 未转发时，门户直接 TLS 跑 80
sudo .venv/bin/python scripts/portal_tls.py disable           # 回退 80 明文（证书保留）
```

> **机制**：`portal.service` 的启动行是 `--port ${WEB_PORT} $TLS_ARGS`，而 systemd 的
> `EnvironmentFile`（`/opt/portal/.env`）**优先于**单元里的 `Environment=` 默认值（已实测），
> 因此 `portal_tls.py` 只改 `.env` 两行即可切换端口与 TLS；`TLS_ARGS` 为空时 systemd 会整段丢弃参数。

> **过渡模式说明**：若端口转发只开了 80，可用 `enable --port 80` 立刻拿到 HTTPS
> （`https://portal.example.com/`），但**明文 http 链接会失效**（浏览器先试 http 会握手失败，且无法 301 跳转），
> 成员需手动输入 https。**推荐仍是打开 443 转发后用标准模式。**

- 证书（**已在 130 预生成，2026-09-11**）：`/opt/portal/tls/{ca.crt,ca.key,server.crt,server.key}`
  （目录 755、证书 644、私钥 600；SAN 含 `portal.example.com`、`192.168.1.130`、`127.0.0.1`）；
  CA 指纹 `13:3D:E4:D2:…:A9`（10 年），服务器证书 2 年；
  续期 `scripts/tls_issue.py --dir /opt/portal/tls --renew`（复用 CA，成员无需重装）。
  已验证：在 130 上以该证书起 TLS 实例，`https /login` 200、`/api/portal/overview` 401、
  未装 CA 时校验失败（证书校验真实生效）。
- **成员设备需安装一次 `ca.crt`**（否则浏览器提示证书不受信任——这不是故障）；CA 有效期 10 年，服务器证书 2 年。
- 启用后：门户在 443（uvicorn TLS，单 worker），80 由 `portal-redirect.service` 做 **301**；
  会话 Cookie 自动加 `Secure`（`TLS_CERT_FILE` 存在即生效）；WS 走 `wss`；飞书回调须含
  `https://portal.example.com/auth/feishu/callback`（**已登记**，与 http 版本并存）。
- CLI：`python wsctl.py --server https://portal.example.com --cafile /path/ca.crt login --method local`。
- 健康巡检：`portal_health.py --base-url https://127.0.0.1 --cafile /opt/portal/tls/ca.crt`（同机直读模式无需令牌）。

## 1. 应急账号管理（**已退役**，阶段 9）

> D2 起生产**无应急账号**：`AUTH_EMERGENCY_ENABLED` 默认关闭，应急登录端点返回 404。
> 断网兜底改用"本站账号密码 + 维护者 `account_ctl.py reset-password`"（§I）。以下内容仅作历史与
> 测试基建参考（`AUTH_EMERGENCY_ENABLED=1` 时才生效）。

```bash
cd /opt/portal
.venv/bin/python scripts/emergency_ctl.py create <用户名> [--note 备注]  # 创建/轮换（旧会话全部废止）
.venv/bin/python scripts/emergency_ctl.py disable <用户名>              # 撤销：禁登录+废会话
.venv/bin/python scripts/emergency_ctl.py enable <用户名>
.venv/bin/python scripts/emergency_ctl.py list
```

- 密码交互输入两次（或环境变量 `EMERGENCY_PASSWORD`，用于自动化）；**不接受命令行参数**；至少 8 字符；**无默认密码**。
- 数据文件 `config/auth/emergency_users.json`（仅 PBKDF2 散列；`AUTH_EMERGENCY_FILE` 可覆盖路径）。
- 不依赖飞书/互联网/管理快照：断网时创建与登录均可用。
- 失败限频：连续 5 次错误锁定 60 秒（`AUTH_EMERGENCY_MAX_FAIL/LOCK_SEC`）。

## 2. 会话与数据文件

| 文件 | 内容 | 说明 |
| --- | --- | --- |
| `config/auth/sessions.json` | 本地用户会话（令牌 sha256 摘要、角色、有效期、复核时间） | 服务重启仍有效；`AUTH_SESSIONS_FILE` 可覆盖 |
| `config/auth/emergency_users.json` | 应急账号散列 | 见 §1 |
| `config/feishu.json` | 飞书正式配置（app_id/回调/tenant_key 白名单；secret 用 `env:` 引用） | 结构同 `config/feishu.example.json`；`FEISHU_*` 环境变量优先 |
| `config/devices.json`、`config/device-presets/`、`config/{presets,motor_presets}.json` | 设备注册表与预设（旧调试台沿用） | 升级不覆盖（`deploy.py` 硬校验） |
| `firmware/` | 已上传固件 | 同上 |
| `.env` | 凭据与运行参数（600） | 不入库、不进包 |

有效期：成员 12h（`AUTH_SESSION_MEMBER_TTL`）、应急 8h（`AUTH_SESSION_EMERGENCY_TTL`）。
备份：用 §C 的脚本（覆盖上表全部文件）。

## 3. 飞书配置

1. `/opt/portal/.env`（或 `config/feishu.json`）：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`（env 注入，不落盘）/ `FEISHU_REDIRECT_URL=http://portal.example.com/auth/feishu/callback` / `FEISHU_ALLOWED_TENANT_KEYS=<tenant_key>`。
2. 开发者后台「安全设置 → 重定向 URL」必须含同值（当前已登记：域名一条 + 130 的 80/8000 各一条）。
   换主机名/端口必须**两处同步**改，否则回调 `code=2000 redirect_uri unmatch`。
3. **`FEISHU_ALLOWED_TENANT_KEYS` 为空 = 拒绝所有组织登录（不默认放行）**。
4. 未配置时：`/auth/feishu/start` 返回 503 明确诊断；应急登录不受影响。
5. 追加回调 URL（保留既有登记项，只增不删）：
   `PATCH /open-apis/application/v7/applications/{app_id}/config`
   body：`{"security": {"add": {"redirect_urls": ["http://<新地址>/auth/feishu/callback"]}}}`（实测 code=0）。

## 4. 访问模型（D1）速查

| 身份 | 管理页面/API | 调试页面/API/WS | 说明 |
| --- | --- | --- | --- |
| 匿名 | 401/302 登录页 | 401/302；WS 4401 | 无旁路（旧/新 API 全覆盖） |
| 应急 | 403（页面 302 login?forbidden） | ✓ | 复核不适用（断网可用） |
| 成员（本站账号或飞书） | ✓ | ✓ | 复核每 300s；退出组织即失效并**删除本地账号** |
| 成员 + 未开通账号 | ✗（302 `/onboarding`） | ✗（403 `onboarding_required`） | 首次飞书登录必须设置本站用户名+密码 |
| 成员+复核不可用（断网） | 403 `recheck_unavailable`（非"被移除"） | **放行**（D2：断网仅调试） | 上游恢复后自动恢复；WS 不关闭 |

WS：握手 4401/4403；连接期每 30s 复查（过期 4408 / 撤销 4403）；`/ws/serial`（含 v2）每条控制指令前即时查撤销。

## 5. CLI（wsctl.py）

```bash
python wsctl.py --server http://portal.example.com login --method local --username <名>  # D2 推荐；密码交互输入或 WSCTL_PASSWORD
python wsctl.py --server https://portal.example.com --cafile /path/ca.crt login --method local  # HTTPS（自签 CA）
python wsctl.py --server http://portal.example.com login --method feishu                     # 打印授权 URL，浏览器完成后回贴 --code --state
python wsctl.py logout
python wsctl.py --device <设备ID> status|serial ...|flash ...|presets ...   # 多设备路由（v2 端点）
```

会话文件 `~/.config/wsctl/session-<hash>.json`（目录 0700 / 文件 0600）；401 时提示重新登录。

## 6. 回退方式

- **前端回退（2026-10 起，最轻量）**：新前端（Vue，`static/app/`）出问题时，在 `/opt/portal/.env` 写
  `PORTAL_FRONTEND=legacy` 后 `sudo systemctl restart portal.service`，页面即回到旧版原生前端
  （`static/portal/`、`static/index.html` 原样保留），接口与数据不受影响；删掉该行再重启即切回。
  前端开发、构建与架构记录见 `docs/frontend-vue.md`。
- **整站回退（推荐，已验证）**：§B.4 的 `--rollback`（停门户、起旧调试台）。旧目录与旧数据未动，
  回退后 80 端口行为与切换前完全一致；再次 `--cutover` 部署即切回门户。
- **回退到阶段 2 匿名态**（历史路径，**仅限受控局域网**）：恢复 `app/main.py`（认证路由与中间件消失）
  + `app/config.py` + `static/app.js`（401 拦截），可参考 `backups/snapshot-phase3-pre-20260909-161712.tar.gz`；
  此时**不得**保留新管理页面同时暴露匿名调试 API。
- 数据回退：用 §C 的备份恢复；**不得**对未知 `schema_version` 的库直接降级读取（程序会拒绝启动数据接口）。

## 7. 常见故障

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 全部请求 401 | 无会话/已过期 | 登录；应急用 §1 建账号 |
| 成员 403 `recheck_unavailable` | 飞书不可达超宽限（≠被移除） | 检查出网/凭据；期间用应急登录调试 |
| `/auth/feishu/start` 503 | 飞书未配置 | §3 |
| 回调 400 state 无效 | 重放/超 10 分钟 | 重新发起登录 |
| 回调 2000 `redirect_uri unmatch` | 登记值与环境变量不一致 | 两处同步改成同一 URL（§3.2/§3.5） |
| WS 立即 4401 | 凭证未随连接发送 | 浏览器查 Cookie；CLI 查会话文件 |
| 服务重启后旧会话失效 | 不应发生（持久化） | 检查 `config/auth/sessions.json` 可写性与路径 env 一致性 |
| 登录后所有页面被弹回 `/onboarding` | 该飞书账号尚未开通本站账号（首次登录需设置用户名+密码） | 按页面提示设置；用户名被占用时换一个 |
| 门户看不到管理数据但调试正常 | 上游复核不可用（断网/飞书故障）——**这是设计行为** | 等复核恢复；期间用调试台，或看 §8 的本地状态查询 |
| 忘记密码且断网 | 无飞书验证通道 | 维护者执行 `account_ctl.py reset-password <用户名>` |
| 浏览器提示证书不受信任 | 自签 CA 未安装 | 安装 `/opt/portal/tls/ca.crt`（或联系维护者分发） |
| 访问 `http://` 被跳转 | 已启用 HTTPS，80 只做 301 | 用 `https://portal.example.com/` |
| 管理数据接口 **503 `schema_version_mismatch`** | 快照库版本与本程序不一致（或库无版本键但已有数据） | §8.1 |
| **断网后门户显示受限/无身份可查** | 断网超过复核宽限后唯一 member 会被收紧为 `403 复核不可用`，此时无 Web 身份可查同步状态 | 服务器本地只读查 meta（**130 上没装 `sqlite3` 命令行**，用 venv 的 python）：`cd /opt/portal && .venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:config/portal_snapshot.db?mode=ro',uri=True);print(dict(c.execute('select key,value from meta')))"` —— 看 `last_success_at`/`last_error`/`counts`/`current_version`；注意 `last_duration_sec` **只在成功轮写入**，失败轮沿用上一成功值，不能当作失败轮耗时。恢复网络后自动回到 `ok` |

## 8. 门户同步与快照

- 数据文件：`config/portal_snapshot.db`（SQLite，`schema_version=2`；env `PORTAL_DB` 可覆盖；**不在静态资源目录**）。
  版本 2（2026-10-21）新增本地缓存库的五张表：飞书镜像 `remote_records`、待推送队列 `outbox`、
  `id_map`、`conflicts`、`attendance_sessions`，设计见 `docs/portal-cache-design.md`。
  每轮同步内容有变时刷新镜像，日志为「飞书镜像已刷新」；刷新失败即整轮同步失败（保留上一版快照），
  并记 meta `remote_mirror_error`。写入模式见 §8.2。
- **快照来源（2026-10-21 起）**：页面快照由「飞书镜像 ⊕ 本地队列里还没推出去的改动」生成。
  网页写入、推送完成后会在本地重建快照，并发布新的 `snapshot_version`，写完立即可见。
  本地重建**不改** `last_success_at`，`stale` 和巡检判断的仍是「上次真正从飞书同步成功」的时间。
- 回退开关：`PORTAL_SYNC_ENABLED=0` —— 不启动后台同步、手动 sync 返回 accepted=false；既有快照与全部调试功能不受影响（管理 API 只读旧快照）。
- 手动刷新：`POST /api/portal/sync`（成员）合并定时任务，全局最小间隔 `PORTAL_MANUAL_MIN_GAP`（默认 10s），前端人数不放大飞书请求。
- 状态观测：`GET /api/portal/sync-status`（snapshot_version / last_success_at / last_error 类别 / syncing / counts）。生产（80）实测：每 ~75s 一轮成功，counts `2/3/2/2`、`quality_issues=0`、`last_error` 空。
- 上游挂起（黑洞）语义：上游「接受连接但不回包」时，一轮同步在 **超时 × (重试+1) + 退避** 后以类别 `network` 有界失败（默认 10s×3 + 退避 ≈ 33s；测试注入 3s×2 ≈ 9.5s 实测），调度线程不会被永久占住；期间调试 API 与既有快照不受影响。
- 内网扫描接口：`POST /api/v2/devices/scan` 会主动探测服务器所在 /24 的 4441/20010 端口；**需登录**（匿名 401），可用 `SCAN_SUBNET` 限定范围（如 `SCAN_SUBNET=192.168.1`），不设则自动推断本机 /24。
- 失败语义：任一表失败 → 丢弃候选保留旧版（stale 依据 `PORTAL_SYNC_STALE`，默认 180s）；错误只记类别不含 URL/令牌。

- 考勤换周（2026-10-20 起）：零点任务（线程 `attendance-midnight`）每天 00:00 作废孤儿签到，进入新的一周时再换周，日志分别为「考勤零点作废完成」「考勤换周完成（YYYY-MM-DD 起）」。执行记录存在快照库 meta 的 `attendance_midnight_last_run` / `attendance_week_last_run`。服务在零点不在线时，启动后补跑（仅当此前执行过）。飞书侧需要先执行一次 `scripts/feishu_attendance_setup.py`，见 `docs/feishu-member-data-guide.md`「周时长结算与值日表」；未执行时换周什么也不写，考勤页提示「未接入」。

### 8.1 快照库版本不匹配（阶段 7 新增：故障关闭）

程序在**任何写入之前**做版本闸门：库里 `schema_version` 缺失（且已有业务数据）或不等于程序
`SCHEMA_VERSION` → 抛 `SchemaVersionError`：数据接口返回 **503**（`code=schema_version_mismatch`，
带 `schema_found/schema_expected/snapshot_path/hint`），同步以类别 `schema` 失败，**零写入**
（连 `PRAGMA journal_mode=WAL` 与建表都不做）。调试台不受影响。

处置顺序：

```bash
systemctl stop portal.service
cd /opt/portal && .venv/bin/python scripts/portal_backup.py \
    --data-dir /opt/portal --out /opt/portal-backups/preupgrade-$(date +%Y%m%d-%H%M)  # 先备份（会标注 ok=false）
# 方案 A：用迁移脚本升库（先 --dry-run 看计划；真正迁移会先备份到 --backup-dir 再迁移）
.venv/bin/python scripts/portal_migrate.py --db /opt/portal/config/portal_snapshot.db --dry-run
.venv/bin/python scripts/portal_migrate.py --db /opt/portal/config/portal_snapshot.db \
    --backup-dir /opt/portal-backups/premigrate-$(date +%Y%m%d-%H%M)
# 方案 B：恢复一份匹配版本的备份
.venv/bin/python scripts/portal_restore.py --bundle <匹配备份> --target /tmp/restore-check --dry-run
systemctl start portal.service
```

**禁止**手改 `schema_version` 或让程序"顺手"补写版本后继续读——那正是本闸门要防的静默误读。

迁移历史与脚本行为：

| 版本 | 日期 | 内容 |
| --- | --- | --- |
| 1 | 阶段 4 | 快照 `snapshot` / 质量报告 `quality` / `meta` |
| 2 | 2026-10-21 | 加本地缓存库五张表（只加不改，旧数据不动） |

- `portal_migrate.py` 退出码：0 已迁移或无需迁移；1 执行出错（单事务已回滚，库保持原样）；
  2 拒绝迁移（版本未知、缺失、比程序新，或没给 `--backup-dir`）。拒绝时库文件零改动。
- 迁移只加新表，可以在服务运行时执行（等写锁最多 `--lock-timeout` 秒，默认 30）。
  `deploy.py` 就是这样做的：旧进程继续跑，迁移完再重启。
- 回退到版本 1 的旧程序：停服务 → 用 `premigrate/portal_snapshot.v1.db` 覆盖库文件并删掉同名
  `-wal/-shm` → 换回旧代码 → 启动。网页写入改走本地队列以后（缓存库第 3 步），回退前须先确认
  备份输出里的「未推到飞书的改动」为 0。

### 8.2 写入模式与本地队列（2026-10-21 起）

- **默认 `PORTAL_WRITE_MODE=cache`（2026-10-29 起）**：网页写入先入本地队列立即返回（响应里
  `queued: true`），推送线程 `portal-push` 按序推到飞书，页面不等飞书 RTT；新建等排队写入返回
  `loc_` 开头的本地临时 ID，推送成功后自动换成飞书 `rec_` ID。
- **默认排队表**：`PORTAL_CACHE_TABLES=tasks,projects,schedule,procurement`。`members` 不在默认列表
  ——考勤签到/签退与零点结算写的是 `members` 列，属关键路径，推送失败会让工时账目不一致，保持直写。
- **回滚**：`Environment=PORTAL_WRITE_MODE=direct`（systemd 附加配置
  `/etc/systemd/system/portal.service.d/write-mode.conf`，或 `.env`），`systemctl daemon-reload &&
  systemctl restart portal.service`，启动日志「门户写入模式 direct」即回到旧行为；队列里未推完的操作
  在 direct 下仍会被推送线程继续排空。
- **改排队表**：`PORTAL_CACHE_TABLES` 逗号分隔，改完同样重启；`.env` 里同名变量优先于附加配置。
  生产上按表逐步切换（第 6 步），建议顺序：日程、事项 → 采购 → 考勤。
- **观测**：`GET /api/portal/sync-status` 的 `cache` 字段（待推 / 推送中 / 失败 / 未确认冲突 / 最老未推送时间）；
  日志关键字「推送新建」「推送修改」「被飞书拒绝」「冲突」「暂时失败」。巡检退出码 8 见 §E。
- **页面上看到的**：
  - 记录上的徽标：「待同步」（正在推，重试中会附上次失败原因）、「同步失败」（这次改动没推上去，
    页面显示的是飞书上的值）、「冲突」（同一列飞书上也改过，已按飞书为准）。详情页显示整句说明。
  - 侧栏同步行带「N 条改动待推送 / N 条推送失败 / N 条冲突待确认」，有失败或冲突时给出入口。
  - 「数据质量」页的「网页改动推送」「冲突」两块：全员可看；**组长**可以重试、放弃（两步确认）、
    确认冲突。重试或放弃一条新建时，同一记录上被连带失败的后续改动一并处理。所有动作记审计日志
    （`portal_queue_retry` / `portal_queue_discard` / `portal_conflict_ack`）。
- **处理推送失败**（`failed`，飞书明确拒绝，例如字段被改名或删掉、选项不存在、权限不足）：
  一般由组长在「数据质量」页处理；需要在服务器上处理时用命令行：

  ```bash
  cd /opt/portal
  .venv/bin/python scripts/portal_queue.py --db config/portal_snapshot.db status
  .venv/bin/python scripts/portal_queue.py --db config/portal_snapshot.db list --state failed
  # 修好飞书侧（或配置）后重试；确认不需要的就作废（作废的改动不会再推到飞书）
  .venv/bin/python scripts/portal_queue.py --db config/portal_snapshot.db retry <编号>
  .venv/bin/python scripts/portal_queue.py --db config/portal_snapshot.db discard <编号> --note "原因"
  ```

- **冲突**：同一列网页和飞书两边都改了，一律以飞书为准，并记一条冲突。组长在「数据质量」页确认；
  也可以用 `portal_queue.py conflicts` 查看（基准 / 网页值 / 飞书值），确认后用 `ack <编号> --by <姓名>`。
  命令行是另一个进程，处理后页面上的徽标要等下一轮同步（≤ 同步间隔）才消失；页面上处理是立即的。
- **切回 direct**：先用 `portal_queue.py status` 确认「待推 0、推送中 0」再改 `.env` 重启。
  考勤的直写路径不经过队列，有积压时切回去顺序无法保证。
- **推送线程卡住或飞书持续不可写**：改动会留在队列里，重启服务也不会丢，恢复后自动推送。
  积压超过 15 分钟，巡检报 8。

## 9. 未完成项与已知限制（阶段 8 更新，如实）

1. **无 HTTPS**（维护者决定）：门户以 `http://portal.example.com/` 提供，凭据与会话 Cookie 走明文；
   若后续要上 HTTPS，需同时改 `.env` 的 `FEISHU_REDIRECT_URL` 与开放平台登记值，并给
   `SESSION_COOKIE` 加 `Secure`（阶段 3 预留）。
2. **硬件动作未实测**：VLLINK USB 配置读写（历史 Pipe error）、TCP-DAP 接 MCU 全链路、
   0x91 厂商配置命令、AP 远端配置转发、同 STA 烧录+串口并行、50Hz 真实遥测——均待授权后实机。
3. **成员身份 UI 走查**：切换后尚未由成员本人在浏览器完成一次完整登录 + 五页走查
   （服务端已验：回调 URL 被飞书接受〔授权页 302 到 passport 登录页〕、匿名码位、资源加载、9/9 官方链接）。
4. ~~令牌缓存每轮重建~~ → **阶段 8 已修**：按响应 `expire` 进程级复用（实测 3 轮同步 0 次 token 请求）。
5. ~~轮询重渲染不恢复键盘焦点；手动刷新限频全局~~ → **阶段 8 已修**：焦点按稳定标识恢复
   （内容被替换时允许回落，不报错）；限频改为**按用户 + 全局下限**（`PORTAL_SYNC_GLOBAL_MIN_GAP`）。
6. ~~阶段 6 遗留的测试侧强化项~~ → **阶段 8 已补**：6-6 增 1 页面对照组（单页面 6s 本地 API 10 次、
   bitable 0 次）、6-7 增"标称 20Hz vs 实测"节奏对照。
