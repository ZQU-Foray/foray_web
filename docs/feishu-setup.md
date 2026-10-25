# 飞书接入配置指南与实证记录（阶段 1 交付）

> 编制日期：2026-09-08 晚 · 依据 `docs/team-portal-migration-plan.md` §4/§6.5/§7 与 §8.1。
> **复验更新：2026-09-08 22:12–22:25 CST（见 §10）**——bitable/contact 权限已实证恢复并生效，99991672 问题解决；四张目标表仍未建立。
> 本文档分三档证据：
> - **已证实**：真实 API 调用成功（注明时间；2026-09-08 21:1x–21:3x 与 22:1x–22:2x CST 两个窗口）或官方文档明文规则；
> - **仅模拟**：仓库匿名 fixture 演练（`tests/fixtures/feishu/`）；
> - **待维护者配置**：需要维护者在飞书后台操作后才能落实的项。
>
> 凭据纪律：**App Secret 只经环境变量 `FEISHU_APP_SECRET` 注入**，不入文档、不入配置文件、不入日志。本文所有示例均脱敏。

---

## 1. 当前配置状态快照（2026-09-08 21:47 CST）

| 项 | 状态 |
| --- | --- |
| 企业自建应用 | 已创建，App ID `cli_xxxx…xxxx`（维护者提供，非机密） |
| App Secret | 已提供（仅存环境变量，本文不含） |
| 应用发布 | 维护者称"已发布" |
| 目标多维表格 | 已授权给该应用（维护者操作）；`app_token` 从链接提取 = `<旧Base app_token>` 形态（Base URL `/base/` 之后） |
| 现有数据表 | **4 张既有表**：项目 `<表ID>`、任务 `<表ID>`、周报 `<表ID>`、成员 `<表ID>`（战队现行管理表，结构见 §7.4） |
| 计划 §4 四张目标表 | **尚未建立**（用户确认） |
| bitable API 权限 | **当前失效**：21:15–21:30 窗口实测全部成功，21:40 起同 token 复测返回 99991672（见 §2.5 现象记录） |
| 通讯录权限 | 未开通（实测 99991672，见 §3） |
| OAuth 重定向 URL | 未配置（应用安全设置未见录入，待维护者添加局域网回调） |

## 2. 已证实的接口与行为

### 2.1 应用身份令牌（已证实，21:15/21:47 两次）

```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
Body: {"app_id": "...", "app_secret": "..."}        # Content-Type: application/json
```
- 成功：`{"code":0, "tenant_access_token":"t-g10…"(42字符), "expire":7200}`；**expire 为剩余秒数**——有效期内重复调用幂等返回同一 token（实测 7200 → 6847 → 6804 递减）。
- 失败（错误 secret）：`code` 非 0（未实验，按文档）。
- 运维含义（阶段 4 输入）：token 缓存至过期前刷新即可，无需每请求获取；2 小时窗口。

### 2.2 多维表格三个只读接口（21:15–21:30 窗口全部实测成功）

| 接口 | 请求 | 实测结果 |
| --- | --- | --- |
| 列出数据表 | `GET /open-apis/bitable/v1/apps/{app_token}/tables?page_size=100&page_token=` | code 0；返回 `items[]{table_id,name,revision}`、`has_more`、`page_token`、`total`；实测 4 表（§1） |
| 列出字段 | `GET .../tables/{table_id}/fields?page_size=100` | code 0；返回 `items[]{field_id,field_name,type,ui_type,property}`；`page_size` 上限 **100** |
| 列出记录 | `GET .../tables/{table_id}/records?page_size=N&page_token=&user_id_type=open_id` | code 0；`page_size` 上限 **500**（文档）+ 实测 2/页翻页正确；返回 `items[].fields`（按字段名组织）、`record_id`、`total`、`has_more` |

**分页实证**（page_size=2，服务端 total=3）：第 1 页 `items=2, has_more=true, page_token=<rec开头>` → 第 2 页 `items=1, has_more=false`；`total` 每页一致 = 3，累计拉取数与 total 相等。接口频率限制：文档标注 **20 次/秒**。

**字段值形状实证**（阶段 4 解析器的契约，全部来自真实响应）：

| 字段类型（type/ui_type） | 记录中的值形状 | 说明 |
| --- | --- | --- |
| 文本 1/Text | `"字符串"` 或 `null` | |
| 单选 3/SingleSelect | `"选项名"` | 字段元数据 `property.options[].name` 可预取选项集 |
| 多选 4/MultiSelect | `["选项名", …]` | |
| 日期 5/DateTime | `1786272585000`（**毫秒时间戳 int**）或 `null` | `property.date_formatter` 标记展示格式（yyyy/MM/dd 等）；**不能从值本身区分纯日期/精确时刻**——这就是 §4 设计"截止时间精度"单选字段的原因 |
| 复选框 7/Checkbox | `true/false` | |
| 人员 11/User | `[{"id":"ou_xxx(35字符)","name","en_name","email"}]` | `id` 类型由 `user_id_type` 参数决定（默认 open_id）；**响应直接内嵌姓名**——同步器不得把 name 原样入日志 |
| 单向关联 18/SingleLink | `[{"table_id","record_ids","text","text_arr","type":"text"}]` | 关联值含目标表 ID + 记录 ID 数组 + 显示文本；解析归属以 `record_ids` 为准，`text` 仅展示 |
| 双向关联 21/DuplexLink | 同上形状 | 语义同 18 |
| 查找引用 19/Lookup | 形状随源列（如 `{"users":[{…,userId}]}`） | **本方案不使用 Lookup 作为数据源**（值随源列类型漂移），仅记录存在 |
| 公式 20/Formula | 计算值（int/str 等） | 不作为数据源 |
| 修改时间 1002 | 毫秒时间戳 | |

**user_id_type 实测**：`open_id` 与 `user_id`（后者文档要求 `contact:user.employee_id:readonly`）在 21:15 窗口均返回 code 0；**统一采用 open_id**（与应用绑定、无需额外权限假设，跨场景稳定）。

### 2.3 授权登录三接口（官方文档已核实，浏览器流程待实测）

| 步骤 | 接口 | 关键规则（文档实证） |
| --- | --- | --- |
| 1. 授权码 | `GET https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id={app_id}&response_type=code&redirect_uri=<URL编码>&scope=<空格分隔>&state=<随机>` | 授权码 **5 分钟有效、仅一次**；`state` 原样回传（CSRF 防护必校验）；拒绝授权回传 `error=access_denied`；支持 **PKCE（S256）**；scope 未在后台申请会报 20027 |
| 2. 用户令牌 | `POST /open-apis/authen/v2/oauth/token`（v2，文档核实；v1 为 `/authen/v1/oauth/token`） | body 换 `code` + `client_id`/`client_secret`（或 PKCE `code_verifier`）；返回 `user_access_token`/`refresh_token`；**需 `offline_access` scope 才有 refresh_token** |
| 3. 用户信息 | `GET /open-apis/authen/v1/user_info`（Bearer user_access_token） | 返回含 **`tenant_key`**（组织校验锚点，计划 §6.5）、`open_id`、`name` 等。**登录只需 user_info 默认授权，授权页 scope 可留空**——不要把应用身份权限（bitable:*/contact:*）或成员复核所需的通讯录 scope 拼进登录 scope；refresh_token（`offline_access`）仅当阶段 3 决定长期续用 user_access_token 时才申请，当前设计（本地会话 + 应用身份复核）不需要 |

> 步骤 1–3 的端到端浏览器流程**未实测**：需要先把局域网回调 URL 录入应用安全设置（写操作，见 §5），列为待维护者配置。

### 2.4 成员状态复核（实测：权限缺失，接口与所需 scope 已明确）

```
GET /open-apis/contact/v3/users/{open_id}?user_id_type=open_id   (Bearer tenant_access_token)
```
- **21:30 实测：code 99991672**——需开通任一接口级 scope：`contact:contact.base:readonly` / `contact:contact:access_as_app` / `contact:contact:readonly` / `contact:contact:readonly_as_app`。
- 响应中 `user.status`（**is_resigned / is_exited / is_frozen / is_activated**——撤权复核所需）另有字段级权限：`contact:user.employee:readonly` 等。
- **方案定性（计划 §6.5 要求"不需要公网入站"）：本接口为服务器主动出站调用，满足要求**。复核节奏：在线会话每 300 秒（配置 `member_recheck.interval_sec`）。
- 最小权限组合建议：接口 `contact:contact.base:readonly` + 字段 `contact:user.employee:readonly`。

### 2.5 ⚠️ bitable 权限"先成功后失效"现象记录（如实，已于复验解决）

- 21:15–21:30 CST：凭据签发的 tenant token 调 tables/fields/records **全部 code 0**（§2.2 全部实证采集于此窗口）。
- 21:40 起至 21:47：**同一 token**（幂等复用，expire 递减可证）复测 tables/fields/records → `code 99991672`，提示应用尚未开通 `bitable:app:readonly` / `bitable:app` / `base:table:read` 任一。
- 期间本方仅发起 GET 与 token 获取，**无任何写操作**。
- **复验结论（22:16–22:24 CST，§10）**：维护者完成权限导入/审批/版本发布后，同一凭据同接口 **code 0 恢复**。定性为：21:40 窗口处于权限变更生效的中间态（权限修改需随版本发布对已发布应用生效），非权限被撤销，也非本方误报。

## 3. 权限矩阵（最小集，按接口逐条）

| 用途 | 接口 | 所需 scope（任一/组合） | 当前应用状态 |
| --- | --- | --- | --- |
| 同步四表（应用身份） | tables/fields/records 三接口 | `bitable:app:readonly`（推荐，只读最小） | **已开通（22:16 复验 code 0）** |
| 成员复核（接口） | contact/v3/users/get | `contact:contact.base:readonly`（推荐） | **已开通（22:2x 复验 code 0）** |
| 成员复核（status 字段） | 同上 | `contact:user.employee:readonly` | **已开通（status 5 键可读）** |
| 通讯录范围自查 | GET /open-apis/contact/v3/scopes | `contact:contact.base:readonly` 任一 | **已开通（22:2x 复验：8 用户/0 部门/0 用户组）** |
| 登录（用户身份） | authen v1/v2 | 授权页 scope 可留空（user_info 默认授权） | 待浏览器实测（回调接口阶段 3 实现） |
| 记录中人员姓名内嵌 | records 返回 | 无额外（bitable 权限即含） | 21:15 窗口已证实随记录返回 |

**Base 资源授权（与 scope 无关的另一层）**：文档 1254302 错误码说明——Base 开启**高级权限**时，还须把应用添加为 Base 协作者（或经群组角色）。当前 Base 已授权给应用（维护者操作 + 21:15 窗口实测通过），**但建新表后若 Base 启用高级权限，需复查协作者范围**。普通权限模式下 scope 即可。

**应用可用范围**：登录与用户身份类能力受"可用范围"（通讯录范围）影响——可用范围内成员才能完成授权；同步走应用身份不受个人范围限制，但 Base 高级权限下协作者角色决定可见数据。范围配置由维护者在发布时确认（§8 第 4 项）。

## 4. 四张目标表数据字典（计划 §4 落地，建表依据）

字段映射语义键 = `config/feishu.example.json` 的 `tables.<表>.fields.<语义键>`；**后端契约使用语义键 → field_id 映射，中文列名仅作飞书端人工维护提示**。选项集写入字段选项；"必填"= 建表时勾选必填。

### 4.1 projects 项目表

| 语义键 | 飞书字段名 | 类型（ui_type） | 规则 |
| --- | --- | --- | --- |
| name | 项目名称 | 文本（主字段） | 必填 |
| description | 项目说明 | 多行文本 | 可空 |
| owner_group | 主责组 | 单选：机械/硬件/电控/算法/运营 | 必填 |
| collab_groups | 协作组 | 多选（同上五项） | 可空 |
| owners | 负责人 | 人员（允许多选） | 可多人 |
| plan_start | 计划开始 | 日期（仅日期格式） | 可空 |
| plan_end | 计划结束 | 日期 | 可空；不早于开始 |
| status | 项目状态 | 单选：筹备中/进行中/已结束/已归档 | 必填（人工维护） |

### 4.2 tasks 事项表

| 语义键 | 飞书字段名 | 类型 | 规则 |
| --- | --- | --- | --- |
| name | 事项名称 | 文本（主字段） | 必填 |
| project | 所属项目 | 关联 → projects 表 | 可空（无项目事项仍入分组列表） |
| owner_group | 主责组 | 单选（五组） | 必填 |
| collab_groups | 协作组 | 多选 | 可空 |
| owners | 负责人 | 人员（多选） | 可多人；空=待分配 |
| priority | P等级 | 单选：P0/P1/P2/P3 | 必填 |
| status | 状态 | 单选：未开始/进行中/受阻/已完成/已取消 | 必填 |
| start_time | 开始时间 | 日期 | 可空 |
| due_time | 截止时间 | 日期 | 可空（纯日期或精确时刻均可） |
| due_precision | 截止时间精度 | 单选：仅日期/精确时刻 | 有截止时间时必填（日期值不含精度信息，§2.2 实证） |
| done_time | 完成时间 | 日期 | 可空；不替代状态判据 |
| description | 事项说明 | 多行文本 | 可空 |
| blocker_note | 阻塞说明 | 多行文本 | 受阻时建议填写 |

### 4.3 milestones 重要节点表

| 语义键 | 飞书字段名 | 类型 | 规则 |
| --- | --- | --- | --- |
| name | 节点名称 | 文本（主字段） | 必填 |
| kind | 节点类型 | 单选：比赛/关键检查 | 必填 |
| target_time | 目标日期时间 | 日期 | 必填 |
| time_precision | 时间精度 | 单选：仅日期/精确时刻 | 必填（避免零点误当开始时刻） |
| project | 所属项目 | 关联 → projects | 可空 |
| visibility | 展示状态 | 单选：展示/隐藏/已取消 | 默认展示 |
| description | 节点说明 | 多行文本 | 可空 |

### 4.4 schedule 公共日程表

| 语义键 | 飞书字段名 | 类型 | 规则 |
| --- | --- | --- | --- |
| name | 日程名称 | 文本（主字段） | 必填 |
| start_time | 开始时间 | 日期 | 必填 |
| end_time | 结束时间 | 日期 | 可空；不早于开始 |
| all_day | 全天 | 复选框 | 全天按 Asia/Shanghai 解释 |
| kind | 活动类型 | 单选：例会/联调/测试/其他 | |
| groups | 相关组 | 多选（五组） | 展示标签，非权限 |
| participants | 参与人 | 人员（多选） | 可空 |
| location | 地点 | 文本 | 可空 |
| project | 所属项目 | 关联 → projects | 可空 |
| description | 日程说明 | 多行文本 | 可空 |
| status | 状态 | 单选：正常/已取消 | |

> 关联字段建议用**单向关联（18）**，双向关联（21）亦被校验工具接受（§2.2 两者值形状一致）。

### 4.5 飞书端建议视图（人工维护用，网页不依赖）

事项：全部 / 按主责组 / P 等级排序 / 未完成 / 甘特；项目：进行中；节点：未来节点；日程：公共日历。网页读取底层记录自行计算（计划 §4.5）。

### 4.6 现有四张表与目标字典的差异（21:15 窗口实测结构）

现有"任务"表（任务/所属项目(21)/状态(未开始|进行中|已完成|**已停滞**)/开始时间/截止时间/优先级(P0-P3)/任务执行人(21→成员表)/实际完成时间）与 §4.2 相近但**不等**：缺主责组/协作组/截止精度/说明字段，状态枚举不同（已停滞 vs 受阻），执行人关联到成员表而非直接人员字段。**结论：按 §4 新建四张目标表**（维护者操作），现有表保持不动（战队现行数据，不在本次改造范围内迁移）。

## 5. 局域网 OAuth 回调与 HTTPS 要求（文档实证 + 待实测项）

官方"配置重定向 URL"规则（`open.feishu.cn/document/uYjL24iN/uYjN3QjL2YzN04iN2cDN`，2026-09-08 核阅）：

1. 回调地址必须**先添加到 开发者后台 > 应用 > 安全设置 > 重定向 URL 列表**（上限 300 条）；列表外地址回调返回 `{"code":2000,"message":"redirect_uri unmatch"}`。
2. **未强制 HTTPS**：文档示例含 `http://www.example.com`；**未要求公网域名/ICP/固定端口**，亦未发现禁止私网地址的规则。
3. URL 中 `?` 与 `#` 及其后缀**不参与列表匹配**（配置路径部分即可）。
4. 授权流程是**浏览器 302 到回调**：飞书服务器无需入站访问实验室网络（计划 §6.5 判断成立）；仅浏览器和后端需要能出站访问飞书。
5. HTTPS 建议：虽未强制，令牌级会话建议可信内网证书（如内部 CA/自签 + 客户端预信）承载 HttpOnly Cookie；无 HTTPS 时 SameSite=Lax + 局域网威胁模型下可接受，**由阶段 3 实施时按实际证书方案定**。

**待实测（维护者配置回调后由阶段 3 执行）**：真实浏览器跳转 `http(s)://<局域网地址>/auth/feishu/callback` 是否被接受（含非 80 端口与 `.lan`/IP 形态主机名）；state 回传一致性；拒绝授权路径。

## 6. 成员复核方案（无公网入站，定稿建议）

- **主方案**：服务端会话持有 open_id → 定时（300s）以应用身份调 `GET /open-apis/contact/v3/users/{open_id}`，读 `status.is_resigned/is_exited`；连续失败进入计划 §6.5 的"暂时无法确认成员身份"降级（暂停受保护请求，不伪造结论）。
- 依据：该接口为**出站 HTTP**，无需 Webhook/公网入站；`tenant_key` 校验在登录时经 user_info 一次性完成，复核不依赖 OAuth 刷新。
- 前置：§3 的通讯录 scope 开通（待维护者）。
- 备选（不采用）：通讯录变更事件订阅需要事件回调 URL（公网入站或中继），违背约束，第一版不使用。

## 7. 维护者操作手册（建表 → 映射 → 验证）

### 7.1 建四张表（飞书网页端，人工）

1. 打开目标 Base（链接见维护者记录）→ 新建数据表，按 §4.1–§4.4 逐张建立：先建 **projects**，再建其余三张（关联字段需要目标表已存在）。
2. 字段类型按字典选择；单选/多选选项**按字典枚举逐项录入**（文字完全一致，如"未开始/进行中/受阻/已完成/已取消"）。
3. 人员字段开启"可多选"；日期字段选择对应格式（仅日期类字段用 yyyy/MM/dd；需要时刻的用含 HH:mm 格式——精度最终由"精度"单选字段声明）。
4. 建议按 §4.5 建维护视图。

### 7.2 生成字段映射

```bash
export FEISHU_APP_SECRET=<受保护方式提供>
FEISHU_APP_ID=<app_id> FEISHU_BASE_APP_TOKEN=<app_token> \
  .venv/bin/python scripts/feishu_verify.py --scan <新表table_id>
```
`--scan` 只读打印该表 `field_id / 字段名 / type / ui_type / 选项`，把 field_id 回填到正式配置（复制 `config/feishu.example.json` 为受保护的真实配置，**不入库**）。

### 7.3 校验

```bash
.venv/bin/python scripts/feishu_verify.py --config <真实配置>
.venv/bin/python scripts/feishu_verify.py --mock        # 无凭据演练（预期：50 PASS + 1 模拟 FAIL）
```
通过标准：C2–C6 全 PASS（C7 在通讯录权限开通前会如实 FAIL）。

### 7.4 现有表结构备查（21:15 窗口实测，供映射参考）

周报表 `<表ID>`：日期(5)/汇报标题(20 公式)/汇报人(11 User)/所属项目(18→周报自关联为项目进展列)/进度内容(1)，0 条记录。
项目表 `<表ID>`：项目名称(1)/项目情况概述(1)/总负责人(11)/项目截止时间(5)/目标(1)/状态(3: 推进中|未启动|已完成)/任务数量(19 Lookup)/任务完成度(20)/成员(19 Lookup)/对应任务(21)/项目进展(18→周报)/最后更新时间(1002)/父记录2(18 自关联)，3 条记录。
任务表 `<表ID>`：见 §4.6，3 条记录。成员表 `<表ID>`：**未读取记录**（成员个人信息表，最小化访问原则，仅表清单中确认存在）。

## 8. 三档清单（阶段 1 结论；§10 复验后更新）

### 已证实（真实接口 / 官方文档明文）
1. tenant_access_token 获取、幂等复用、expire 语义（§2.1；22:16 复验同）。
2. bitable tables/fields/records 三接口的行为：分页参数与上限（100/100/500）、20 QPS、has_more/page_token/total 语义、多页翻页正确性（§2.2，21:15–21:30 窗口）。
3. 九类字段值形状（含人员内嵌姓名、关联 record_ids 结构、日期毫秒时间戳）（§2.2）。
4. `user_id_type=open_id/user_id` 均可用（21:15 窗口）；方案统一 open_id。
5. OAuth 授权码规则：5 分钟一次性、state 回传、PKCE S256、scope 空格拼接、20027（scope 未申请）（官方文档）。
6. 重定向 URL 规则：安全设置列表制（≤300）、不强制 HTTPS、`?`/`#` 后缀不匹配、2000 unmatch（官方文档）。
7. 成员复核接口路径与所需 scope 集合（99991672 错误体明示，§2.4）。
8. Base 高级权限与应用协作者的第二层授权关系（1254302 文档）。
9. 现有 Base 的四张既有表结构与差异（§4.6/§7.4）。
10. **（复验新增）bitable 权限已生效**：tables 列表 code 0（22:16/22:24 两轮）。
11. **（复验新增）contact 权限与通讯录授权范围**：`GET /open-apis/contact/v3/scopes` code 0，范围为 **8 名用户、0 部门、0 用户组**（按用户授权形态）。
12. **（复验新增）成员复核 status 字段可读**：对范围内成员 `contact/v3/users/{open_id}` code 0，status 含 is_activated/is_exited/is_frozen/is_resigned/is_unjoin 五键（is_resigned=False, is_exited=False 为正常在职样本）；name/department_ids 未返回——符合最小权限（复核无需姓名）。

### 仅模拟（tests/fixtures/feishu/ 匿名数据）
1. 四张目标表的字段元数据与两页分页记录样本（`tables.json`/`fields.json`/`records.json`）。
2. 校验工具全检查链路（--mock：50 PASS + 1 模拟 FAIL + 1 SKIP）。
3. OAuth 端到端流程的数据流（构造参数正确性）——真实浏览器流程未走。

### 待维护者配置（复验后剩余项，按优先级）
1. ~~恢复 bitable 权限~~ **已完成（复验实证）**。
2. ~~开通通讯录权限~~ **已完成（复验实证：接口 + status 字段均生效）**。
3. **建四张目标表**（§7.1）并回填映射（§7.2）——Base 中仍只有 4 张既有表（22:24 复验确认），这是阶段 4 数据同步的硬阻塞。
4. **提供已登记回调 URL 的具体值**：维护者称已登记，但值未提供——本地无法核对一致性，应用后台的重定向 URL 列表亦无只读 API 可查。请把登记地址（完整 `http(s)://host[:port]/path`）告知后填入正式配置，阶段 3 实现回调接口时实测。
5. **提供 tenant_key**：仍依赖首次真实 OAuth（user_info 响应）或管理后台查询；`allowed_tenant_keys` 保持空 = 登录拒绝所有组织（不接受默认放行）。
6. **确认 8 名授权用户即战队全体成员**：范围为 8 用户（复验实证），名单对应关系维护者自查（本方不读取成员姓名）。

## 9. 阶段 4 同步器实现要点（本文档契约移交）

1. 令牌：tenant token 缓存复用，过期前 ~5 分钟刷新；并发刷新需单飞（single-flight）。
2. 分页：page_size=100（fields/tables）/ 500 上限内按需；循环读至 `has_more=false`；以 `total` 对账（工具已验证该语义）。
3. 限流：全局 ≤20 QPS（文档值），四表 60 秒周期远低于限；429/1254290 退避重试。
4. 解析：日期=毫秒时间戳（**UTC 语义，按 Asia/Shanghai 展示换算；纯日期边界用次日 00:00 排他界**，计划 §5.2）；人员取 `id`（open_id），姓名仅展示不入日志；关联取 `record_ids[0]`（单关联）。
5. 错误分类：99991672=权限缺失（配置告警，非数据为空）；1254302=Base 高级权限；1254030=高级权限未开；2000=回调未配；5 分钟授权码过期仅登录路径。
6. 安全：日志脱敏复用 `scripts/feishu_verify.py` 的 `mask()`；`tenant_key` 与 open_id 不视为机密，但姓名/邮箱按成员信息处理，不入日志。

---

## 10. 复验记录（2026-09-08 22:12–22:25 CST，维护者配置后）

维护者声称完成：①导入 bitable:app:readonly / contact:contact.base:readonly / contact:user.employee_id:readonly；②权限审批与版本发布；③可用范围与通讯录范围覆盖战队成员；④回调 URL 已登记。逐项真实结果：

| # | 声称 | 复验方式（只读） | 结果 |
| --- | --- | --- | --- |
| ①② | 权限导入+审批+发布 | `tenant_token` → `GET tables`（需 bitable 权限）→ `GET scopes`（需 contact 权限）→ `GET users/{id}`（status 需字段级权限） | **通过**：22:16/22:24 两轮 tables code 0（99991672 消失，§2.5 中间态结论成立）；scopes code 0；users/{id} code 0 且 status 5 键齐全。§2.2 全部结构证据持续有效 |
| ③ | 通讯录范围覆盖成员 | `GET /open-apis/contact/v3/scopes`（page_size=100 全量） | **通过（范围非空）**：8 用户 / 0 部门 / 0 用户组。"8 人=战队全体"的对应关系由维护者自查（本方不读姓名） |
| ④ | 回调 URL 已登记 | 无只读 API 可查后台登记列表；本地亦无 URL 值可核对 | **未验证**：请提供登记的完整 URL 值（填入正式配置 `oauth.redirect_url`），待阶段 3 回调接口实现后浏览器实测 |

**最终工具输出（`scripts/feishu_verify.py` 真实模式，22:24:49）**：PASS 4（C2 凭据 / C3 Base+4 既有表 / C7a 范围非空 / C7b status 可读）· FAIL 0 · SKIP 5（C4–C6 四表映射×4：目标表未建；C8：redirect_url 无值）· WARN 0，exit 0。**按复验规则明示：exit 0 ≠ 验收完成，SKIP 项即未验证项。**

### 环境备注（如实区分错误类型）

- 22:12–22:15 本机对 open.feishu.cn 出现 TCP/TLS 间歇性不可达（代理 fake-ip 链路抖动，github 等外网站点正常）——属**网络故障**，与飞书配置无关；已通过在工具 HTTP 层增加退避重试（网络类异常 1/2/4/8/16s 五次重试，API 业务错误不重试）兜底，复验全程未因此中断。
- 未把任何一次网络超时解释为权限问题；未把 API code 99991672 解释为网络问题。

### 本轮工具与配置修正（附验证）

| 改动 | 内容 | 验证 |
| --- | --- | --- |
| `scripts/feishu_verify.py` C7 判定 | code=0 但 status 缺 is_resigned/is_exited → FAIL（不再算通过） | mock 输出不变（无回归）；真实 C7b PASS 以五键齐全为据 |
| C7 人员来源 | 仅三来源：`--test-user` 显式指定 > 四张目标表人员引用 > 官方 scopes 范围首个用户；**移除扫描既有业务表的回退** | 真实运行来源显示"scopes 范围"（合规） |
| C5 增强 | 枚举齐全性（字典枚举缺失→FAIL，额外自定义→WARN）+ project 关联必须指向目标项目表 | mock 输出含"枚举N项齐全/→指向目标项目表" |
| C2 错误分类 | 网络（code=-1）与凭据拒绝分开报告 | 网络抖动期间输出"网络不可达（非凭据/权限问题）" |
| 网络重试 | `_http_retry` 退避（见上） | 真实运行多次自动恢复 |
| 汇总提示 | SKIP/WARN 时明示"exit 0 ≠ 验收完成" | 真实/mock 输出均含提示 |
| `config/feishu.example.json` | 登录 `scope` 改为空串并注明：登录只需 user_info 默认授权；通讯录/employee_id 类 scope 属应用身份（复核）用途，不进登录 scope；offline_access 当前不需要 | JSON 可解析（工具 C1 加载通过） |
| `allowed_tenant_keys` 注释 | 明确禁止空值/通配=放行任意组织；未取得时登录保持关闭并标注 | 同上 |

### 阶段 3 / 阶段 4 前置差距（复验后最新）

- **阶段 3（登录/访问控制）尚缺**：a) 登记回调 URL 的具体值（写入 `oauth.redirect_url` 后才谈得上核对一致）；b) `tenant_key`（首次 OAuth 或管理后台取得，当前为空=拒绝所有组织，不得默认放行）；c) 阶段 3 回调接口本身未实现（本轮只做前置检查，未宣称登录通过）。通讯录权限与复核链路（③）已就绪。
- **阶段 4（数据同步）尚缺**：a) ~~四张目标表未建立~~ → **2026-09-09 已建（见 §11）**；b) ~~回填正式映射~~ → **已写入 `config/feishu.json`**；c) ~~目标表录入人员记录或指定测试成员~~ → 示例数据已写入（不含人员字段，C7 走 scopes 范围来源）。bitable 权限与分页语义（①②）已就绪。

---

## 11. 飞书侧 API 落地记录（2026-09-09，维护者授权后执行）

> 本节为**已证实**（真实 API 写/读，非模拟）。执行方式：tenant_access_token + 官方写接口；App Secret 仅经环境变量注入，未落盘。

### 11.1 目标 Base 变更（重要）

本次建表的目标 Base 由维护者提供：`<测试Base app_token>`（`https://<租户>.feishu.cn/base/<测试Base app_token>`）。
**该 Base 与本文 §1/§7.4 记录的 Base（`<旧Base app_token>`，含 4 张既有业务表）不是同一个**：本 Base 建表前仅有 1 张默认表「数据表」(`<表ID>`，单字段「文本」`<字段ID>`)，未做任何改动。
→ §4.6/§7.4 的既有表结构证据仍有效（历史实测），但**不适用于本 Base**；门户数据源以本节四张新表为准。

### 11.2 四张目标表（已建立，真实 table_id / field_id）

| 表 | 表名 | table_id |
| --- | --- | --- |
| projects | 项目 | `<表ID>` |
| tasks | 事项 | `<表ID>` |
| milestones | 重要节点 | `<表ID>` |
| schedule | 公共日程 | `<表ID>` |

字段映射（语义键 → field_id，已写入 `config/feishu.json`）：

- **projects**：name `<字段ID>`、description `<字段ID>`、owner_group `<字段ID>`、collab_groups `<字段ID>`、owners `<字段ID>`、plan_start `<字段ID>`、plan_end `<字段ID>`、status `<字段ID>`
- **tasks**：name `<字段ID>`、project `<字段ID>`、owner_group `<字段ID>`、collab_groups `<字段ID>`、owners `<字段ID>`、priority `<字段ID>`、status `<字段ID>`、start_time `<字段ID>`、due_time `<字段ID>`、due_precision `<字段ID>`、done_time `<字段ID>`、description `<字段ID>`、blocker_note `<字段ID>`
- **milestones**：name `<字段ID>`、kind `<字段ID>`、target_time `<字段ID>`、time_precision `<字段ID>`、project `<字段ID>`、visibility `<字段ID>`、description `<字段ID>`
- **schedule**：name `<字段ID>`、start_time `<字段ID>`、end_time `<字段ID>`、all_day `<字段ID>`、kind `<字段ID>`、groups `<字段ID>`、participants `<字段ID>`、location `<字段ID>`、project `<字段ID>`、description `<字段ID>`、status `<字段ID>`

建表方式：`POST /open-apis/bitable/v1/apps/{app_token}/tables`，一次性带 `fields[]`（先建 projects，其余三张的「所属项目」为单向关联 `property.table_id=projects`、`multiple=false`）。日期字段按 §7.1 约定选择 `date_formatter`（仅日期用 `yyyy/MM/dd`；含时刻用 `yyyy-MM-dd HH:mm`）。

### 11.3 企业标识（tenant_key，已取得）

`GET /open-apis/tenant/v2/tenant/query` → `tenant_key = <tenant_key>`（企业名：肇庆学院RoboMaster战队Foray，团队版）。
**已写入 `config/feishu.json` 的 `allowed_tenant_keys`**——无需再等首次 OAuth；登录的组织校验锚点已就绪。

### 11.4 权限现状（本次实测）

| 能力 | 状态 | 实测方式 |
| --- | --- | --- |
| 建表 / 写字段 / 写记录（`bitable:app` 或细粒度） | ✅ 可用 | 4 张表 + 9 条示例记录全部 code 0 |
| Base 文档权限 | ✅ 可写 | 同上（应用在该 Base 上具编辑权限） |
| `tenant:tenant:readonly` | ✅ 可用 | tenant/query code 0 |
| `application:application:patch` | ✅ 可用 | `PATCH /open-apis/application/v7/applications/{app_id}/config` code 0 |
| contact 接口 + status 字段 | ✅ 可用 | 见 §2.4/§10 |

### 11.5 示例数据（维护者授权后写入，可删除）

| 表 | 条数 | record_id |
| --- | --- | --- |
| projects | 2 | `<记录ID>`、`<记录ID>` |
| tasks | 3 | `<记录ID>`、`<记录ID>`、`<记录ID>` |
| milestones | 2 | `<记录ID>`、`<记录ID>` |
| schedule | 2 | `<记录ID>`、`<记录ID>` |

全部记录名以「示例…」开头并注明「（示例数据，可删除）」；**未写入任何人员字段**（不触碰真实成员）。用途：验证目标表分页读取、关联值与日期/枚举写入形状。

### 11.6 回调 URL（已登记）

维护者提供：主机 `192.168.1.130`、HTTP、端口按项目设计（systemd 部署 80 / `run.sh` 开发 8000）。经 `PATCH /open-apis/application/v7/applications/{app_id}/config` 登记（**立即生效**）：

- `http://192.168.1.130/auth/feishu/callback`（**生产主值**，写入 `config/feishu.json`；对应 `deploy/webserver.service` 的 80 端口）
- `http://192.168.1.130:8000/auth/feishu/callback`（开发/`run.sh` 备用，`app/config.py` 默认 `WEB_PORT=8000`）
- `http://localhost:8000/auth/feishu/callback`（本机联调用，可随时用 `security.remove.redirect_urls` 移除）

**换主机名/端口/改 HTTPS 时**：登记值与 `oauth.redirect_url` 必须同步更新，否则回调返回 `code=2000 redirect_uri unmatch`。**仍待实测**：真实浏览器跳转（需门户在该地址运行 + 成员本人点授权）。

### 11.7 校验结果（真实模式，2026-09-09）

`.venv/bin/python scripts/feishu_verify.py --config config/feishu.json` → **PASS 52 / FAIL 0 / SKIP 0 / WARN 1**（exit 0）。
- C4/C5/C6 四表：字段类型、枚举齐全性、关联指向目标项目表、分页对账（含 3 条 → 2 页真实翻页）全部 PASS（原为 4×SKIP）。
- C7：通讯录范围 8 用户非空 + status 五键可读 PASS。
- C8：`redirect_url` 形状检查 PASS；**WARN 仅剩「真实浏览器跳转未实测」**（原 SKIP 已消除）。
- `config/feishu.json` 为**受保护正式配置**（600、不含密钥、Secret 走 `FEISHU_APP_SECRET`），不入库、不复制进示例文件。

### 11.8 剩余待办（本次后最新）

1. ~~最终回调 URL 值~~ → **已登记（§11.6）**。
2. ~~真实浏览器 OAuth 实测~~ → **2026-09-09 21:35 已通过**：门户在 `192.168.1.130:8000` 运行，成员浏览器完成授权（`/auth/feishu/start` → 飞书授权页 → `/auth/feishu/callback` → `/api/auth/me` 200）；会话落库 `role=member`、`tenant_key=<tenant_key>`（与白名单一致）、12h 有效期、成员复核已执行。证据见 `docs/portal-progress.md`「真实 OAuth 实测通过」。
3. ~~版本发布~~ → **已提交**：`POST /open-apis/application/v7/applications/{app_id}/publish` 成功，生成 **版本 1.0.4**（`version_id=oav_7c162f5b404bf2117a420cd25903d11c`），**待企业管理员审批**（审批后线上生效；重定向 URL 本身已即时生效，不依赖审批）。
4. 应用可用范围 / 通讯录权限范围：维护者决定**本轮不调整**（当前通讯录范围 8 用户）。
5. **凭据轮换**：本次 App Secret 曾出现在对话记录中，建议联调完成后轮换一次。
6. 发布接口经验记录：`version` 若低于当前线上版本会报 `50516 invalid request`（本次线上已是 1.0.3）；不传 `version` 由平台自动取下一版本即可，`pc/mobile_default_ability` 仅在应用开启了小程序/机器人/网页能力时才必填。
