# NAS 容器化发布与飞书联调（2026-11-13）

> 用途：把 `dev` 分支的门户以容器方式跑在 NAS 上，接真实飞书多维表格，供成员试用。
> 生产服务器（有 systemd、`/opt/portal`）依旧走 `deploy.py` + `docs/portal-deploy.md`；
> 本文只覆盖 NAS 容器这一套，两者共用同一份代码与同一份 `config/` 结构。

## 1. 形态与差异

| 项 | 生产服务器 | NAS 容器 |
| --- | --- | --- |
| 发布工具 | `deploy.py`（SSH + systemd） | `docker compose`（`Dockerfile`、`compose.yaml`） |
| 进程管理 | `portal.service` 单 worker | 容器单进程 `uvicorn --workers 1` |
| 代码位置 | `/opt/portal` | 镜像 `/app`（代码只读，配置挂载） |
| 配置与数据 | `/opt/portal/config`（永不覆盖） | 宿主机 `./config` → 容器 `/app/config` |
| 前端 | `static/app`（服务器不装 node） | 同左，镜像里直接用仓库产物 |
| 端口 | 80/443 | 容器内 8000，宿主机端口可改 |

`deploy.py` **不能**用在 NAS 上：它按 `systemctl` 重启服务、写 `/etc/systemd/system`，而群晖 DSM / 威联通 QTS 没有 systemd。

## 2. 前置条件

- NAS 装了容器套件（群晖 Container Manager、威联通 Container Station，或任意 `docker` + `docker compose`）；
- NAS 上有一个可写的共享目录，下文以 `/volume1/docker/foray` 为例；
- 能 SSH 到 NAS（可选，但排障几乎必须）；
- 浏览器能访问的地址：内网 `http://<NAS IP>:8000`，或已做好 DDNS/反向代理的域名（飞书回调必须是这个地址，见 §3.2）。

## 3. 一次性准备

### 3.1 代码与目录

NAS 能访问 GitHub：

```bash
ssh <用户>@<NAS地址>
mkdir -p /volume1/docker/foray && cd /volume1/docker/foray
git clone git@github.com:ZQU-Foray/foray_web.git src   # 或 https 方式并配 token
cd src && git checkout dev
```

NAS 不能访问 GitHub，就从开发机推过去（不要带 `config/`、`firmware/`、`tls/`、`.venv`）：

```bash
rsync -av --delete \
  --exclude '.git' --exclude '.venv' --exclude 'web' --exclude 'tests' \
  --exclude 'backups' --exclude '参考' --exclude 'config' --exclude 'firmware' \
  --exclude 'tls' --exclude '.dsh' --exclude '.dsh-tmp' \
  ./ <用户>@<NAS地址>:/volume1/docker/foray/src/
```

在代码目录里建好挂载源目录：

```bash
cd /volume1/docker/foray/src
mkdir -p config firmware tls
```

### 3.2 飞书接入（关键，顺序别乱）

1. **应用凭证**：打开飞书开放平台 → 企业自建应用 → 凭证与基础信息，记下 App ID，重置并保存 App Secret（只在 `.env` 里用）。
2. **权限**：给应用开通并**发布新版本**（权限变更不发版不生效）：
   - 多维表格：`bitable:app:readonly`（只读门户所需；若用字段级只读，确认能读到 `records`）；
   - 通讯录：`contact:user.base:readonly` / `contact:user.employee_id:readonly`（成员复核与 open_id 解析）；
   - 应用可用范围要覆盖试用的成员。
3. **重定向 URL**：安全设置 → 重定向 URL 增加 NAS 上这一条，必须与 `.env` 的 `FEISHU_REDIRECT_URL` **逐字一致**（协议、端口、路径都要一致）：

   ```
   http://<NAS地址>:8000/auth/feishu/callback
   ```

   飞书支持 http 与自定义端口；但登录时是浏览器跳过去，所以这个地址必须从成员的电脑/手机上打得开。要做外网访问，就换成反代后的 `https://<域名>/auth/feishu/callback` 并把反代指向 `NAS:8000`。
4. **多维表格授权**：把目标 Base（多维表格）添加该应用为协作者（可编辑权限不够也能只读同步，但授权必须做）。
5. **tenant_key**：`FEISHU_ALLOWED_TENANT_KEYS` 留空时门户**拒绝所有飞书登录**。取值方式：管理后台 → 企业信息 → 企业编号，或首次 OAuth 回调 `user_info` 响应里的 `tenant_key`。

### 3.3 `.env`

```bash
cp deploy/nas.env.example .env
chmod 600 .env
vi .env      # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_REDIRECT_URL / FEISHU_ALLOWED_TENANT_KEYS
```

要点：

- `WEB_PORT` 同时是宿主机映射端口（`compose.yaml` 的 `ports`）和容器内端口，保持 8000 即可；
- `SCAN_SUBNET` 容器里必须显式填写（否则取到的是 docker 网段），不用调试台扫描可以留空；
- NAS 上有反向代理时，把 `FORWARDED_ALLOW_IPS` 改成反代容器地址，否则客户端 IP 会变成反代 IP，考勤网段校验会误判。

### 3.4 `config/feishu.json`

```bash
cp config/feishu.example.json config/feishu.json
vi config/feishu.json
```

必须替换的项：

| 字段 | 取值来源 |
| --- | --- |
| `app_id` | 开放平台凭证页（可入配置，非机密） |
| `app_secret_ref` | 保持 `env:FEISHU_APP_SECRET`（密钥只在 `.env`） |
| `allowed_tenant_keys` | 同 `.env` 的 tenant_key，写成数组 |
| `oauth.redirect_url` | 同 `FEISHU_REDIRECT_URL` |
| `base.app_token` | Base 链接中 `/base/` 之后那一段（wiki 形态先用知识库接口换 `obj_token`） |
| `tables.*.table_id` | 各数据表 ID（`tbl` 开头） |
| `tables.*.fields.*` | 各语义字段对应的**字段 ID**（`fld` 开头），不是中文列名 |

字段 ID 用仓库自带脚本读，别手抄：

```bash
docker compose run --rm portal python scripts/feishu_verify.py --config config/feishu.json --scan tblXXXX
```

建表与回填字段 ID 的脚本（幂等，可重复跑）：`scripts/feishu_attendance_setup.py`、`scripts/feishu_procurement_setup.py`、`scripts/feishu_member_sync.py`；正式 Base 搭建流程见 `docs/feishu-formal-base-guide.md`。

## 4. 构建与启动

```bash
cd /volume1/docker/foray/src
docker compose up -d --build
docker compose logs -f --tail=100 portal
```

启动成功时日志里应出现：

```
服务已启动: http://0.0.0.0:8000（门户写入模式 ...）
门户同步完成 请求数=… 耗时=…  counts={...}
```

若还没配飞书，会看到 `门户同步失败[config]: 飞书同步未配置` —— 服务本身是活的，可以先把飞书配好。

## 5. 首次登录与验证清单

1. **本站账号**：D2 起没有命令行建账号，**首次飞书登录会跳到 `/onboarding`**，由成员自己设置本站用户名与密码（只存 PBKDF2 散列）。之后登录方式 = 本站用户名密码 + 飞书开通状态复核。
2. 账号维护只用于善后（密码不走命令行）：

   ```bash
   docker compose exec portal python scripts/account_ctl.py list
   docker compose exec portal python scripts/account_ctl.py reset-password <用户名>
   ```

3. 探针（容器内执行或从宿主 curl 宿主机端口）：

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://<NAS地址>:8000/login        # 200
   curl -s -o /dev/null -w '%{http_code}\n' http://<NAS地址>:8000/api/status   # 401
   curl -s -o /dev/null -w '%{http_code}\n' http://<NAS地址>:8000/app/portal.html  # 200
   ```

4. 登录后按这条线走一遍，全部绿了才算接上：
   - `/` → 工作台（默认首页），右侧「同步状态」不是红色；
   - `/overview` → 总览，项目/事项数量与飞书表一致；
   - `/attendance` → 考勤卡能看到自己的工时；
   - 工作台「我的考勤」卡片点签到 → 飞书 members 表出现/更新该行；再点签退 → 时长 +0.0x h；
   - `/mcp-guide` → 「网页一键生成」能生成 70 字符 `agent_...` 令牌，刷新后明文消失，撤销后立即失效；
   - `docker compose logs portal | grep 门户同步完成` 里 `counts` 与表数一致、`last_error` 为空。

5. 别在 NAS 上跑 e2e：`tests/` 里全是浏览器套件，容器不需要，CI 也不在这里。

## 6. 升级与回滚

```bash
cd /volume1/docker/foray/src
git fetch && git checkout dev && git pull --ff-only
docker compose up -d --build          # 重建镜像；config/ 挂载不动
```

回滚：

```bash
git checkout <上一个已知可用的 commit>
docker compose up -d --build
```

`config/`、`firmware/`、`tls/` 在宿主机上，重建容器不会丢；要更保险就先备份：

```bash
tar czf /volume1/docker/foray/backup-$(date +%F-%H%M).tgz config firmware
```

## 7. 容器里的已知限制（不是缺陷）

- **烧录/调试/串口**：容器拿不到宿主的 USB 与 OpenOCD。只有要调试台时才需要 `--device /dev/ttyACM0` 并在镜像里装 openocd（默认不装）。门户本身（项目/事项/考勤/采购/MCP）完全可用。
- **设备扫描**：`SCAN_SUBNET` 必须显式配置，且容器用 bridge 网络时扫描的是容器网段，扫描功能在容器里基本不可用；需要的话给容器 `network_mode: host`（群晖上部分场景不支持）。
- **客户端 IP**：考勤的网段校验依赖真实客户端 IP。装了反代就要设 `FORWARDED_ALLOW_IPS`，直连则保持 `127.0.0.1`。
- **单 worker**：会话、镜像缓存、限频与考勤内存状态都在进程内，**不要**加 `--workers` 或多副本。

## 8. 排障速查

| 现象 | 原因与处理 |
| --- | --- |
| 回调回来 `code=2000 redirect_uri unmatch` | 开放平台登记的重定向 URL 与 `.env` 的 `FEISHU_REDIRECT_URL` 不一致（协议/端口/路径差一个字都不行） |
| 登录后所有页面 302 到 `/onboarding` | 该飞书账号还没建本站账号，按页面提示设置用户名密码 |
| 同步日志 `门户同步失败[config]` | `config/feishu.json` 没放对位置或字段没填全，用 `feishu_verify.py` 逐项看 |
| `99991672` 权限错误 | 应用权限没开或**开了没发版**，或 Base 没把应用加为协作者 |
| 登录一律被拒 | `FEISHU_ALLOWED_TENANT_KEYS` 为空 |
| 健康检查一直 unhealthy | 容器内 `/login` 不是 200：看 `docker compose logs`，多半是 `config/` 权限不可写（首启要写 `presets.json`） |
| 页面样式/脚本 404 | 镜像里 `static/app` 缺产物：确认构建前仓库里有 `static/app/assets/portal-*.js`（前端走 `npm run build` 后入库） |
| 构建卡在 `load metadata for docker.io/library/python:3.10-slim`，报 `401 Unauthorized` | NAS 上配置的镜像加速器（如 `docker.fnnas.com`）对 docker.io 鉴权失败，见 §9 |

## 9. 镜像拉不下来（registry 401）

现象：

```
=> ERROR [internal] load metadata for docker.io/library/python:3.10-slim
failed to solve: ... unexpected status from HEAD request to https://<加速器>/v2/library/python/manifests/3.10-slim?ns=docker.io: 401 Unauthorized
```

这不是 Dockerfile 的问题，而是构建机上配置的镜像加速器拒绝了对 docker.io 的匿名拉取。两条路，任选其一。

**路线 A：修 NAS 的镜像源（推荐，一次修好所有镜像）**

1. 先确认是本机 Docker 的问题而不是项目的问题：

   ```bash
   docker pull python:3.10-slim      # 同样 401 → 镜像源问题
   ```

2. 在 NAS 上改加速器配置（群晖：Container Manager → 注册表 → 设置；飞牛 fnOS：Docker → 镜像加速；通用：`/etc/docker/daemon.json`）：

   ```json
   {
     "registry-mirrors": ["https://dockerproxy.net", "https://hub.rat.dev"]
   }
   ```

   把返回 401 的地址删掉；加速器全部不可用且 NAS 能直连外网时，干脆清空 `registry-mirrors`。
3. 重启 Docker 服务后复测 `docker pull python:3.10-slim`。第三方加速器只作过渡，能直连就别长期依赖；对安全敏感时可以拉取后按 digest 固定。

**路线 B：只改本项目的构建参数（不动 NAS 全局配置）**

`Dockerfile` 的基础镜像已经参数化，`.env` 里加一行指向某个拉得到的等价地址即可：

```ini
# 例：阿里云个人加速器 / 其它可用源，注意必须包含 library/python
PYTHON_IMAGE=docker.cn-hangzhou.aliyuncs.com/library/python:3.10-slim
```

```bash
docker compose build --no-cache portal   # 或 docker compose up -d --build
```

镜像名必须保留 `python:3.10` 这个 tag（3.11/3.12 也能跑，但生产是 3.10.12，尽量对齐）。
只换拉取来源、不换版本，因为 `requirements.txt` 里的 `pyudev`、`pyserial` 等在本机是按 3.10 验证的。

