# 工作台（2026-11-03 上线；2026-11-13 起为门户默认首页）

队员自己挑卡片、自己排顺序的首页面板。布局**按人存在服务端**，换设备进来看到的是同一份；
卡片数据仍由服务端按当前布局现算，口径与各页面一致，不新开数据源、不额外拉飞书。

- 页面：`/`（门户默认首页；侧栏 PERSONAL 分组编号 07，原名 `/workbench` 作为别名保留）。
  其余分组顺延：「总览」在 `/overview`（MANAGEMENT 01）、数据质量 `/quality`（08）、MCP 接入 `/mcp-guide`（09）。
- 后端：`app/portal/workbench.py`（卡片目录 + 布局存储 + 数据组装）、`app/portal/api.py` 的
  `GET/PUT/DELETE /api/portal/workbench`。
- 前端：`web/src/portal/pages/Workbench.vue`。
- 库：快照库 `workbench_layouts` 表，schema 版本 **3**（版本 3 只新增这一张表）。
- 测试：`tests/test_workbench.py`。

## 1. 卡片目录（服务端唯一事实源）

前端只渲染服务端下发的清单，卡片是「谁」只有 `workbench.py` 说了算。

| 卡片 id | 标题 | 默认宽度 | 内容 |
| --- | --- | --- | --- |
| `my_attendance` | 我的考勤 | half | 本周时长 / 达标线 / 今日签到状态 / 待结算与请假 |
| `my_tasks` | 我的事项 | full | 名下未完成事项，按截止时间排序（最多 8 条 + 总数） |
| `urgent_tasks` | 临期与逾期 | full | 全队最紧急的未完成事项（`compute.urgent_tasks(limit=5)`） |
| `project_progress` | 项目进度 | half | 在研项目完成率（排除已结束/已归档，最多 6 个） |
| `milestones` | 近期里程碑 | half | 未来节点与倒计时（`compute.future_milestones(limit=5)`） |
| `duty` | 今日值日 | half | 当天值日成员与来源（按日期 / 按星期） |
| `attendance_rank` | 工时排行 | half | 本周打卡时长前五 |
| `my_procurement` | 我的采购 | full | 我作为经手人的采购，按采购日期倒序（最多 5 条 + 总数） |
| `data_quality` | 数据质量 | half | 异常记录总数与按表分布 |
| `sync_status` | 同步状态 | half | 上次成功同步、stale、最近错误、推送积压 |

默认布局（首次进入、点「恢复默认」时）：我的考勤、我的事项、临期与逾期、项目进度、近期里程碑。

## 2. 布局存储与迁移

```sql
CREATE TABLE workbench_layouts(
  open_id TEXT PRIMARY KEY,
  layout  TEXT NOT NULL,      -- JSON: [{"card_id": "...", "width": "half|full"}, ...]
  updated_at REAL NOT NULL);
```

- `layout` 是**有序数组**，顺序即面板顺序；`width` 只在 `half` / `full` 里取。
- 保存前整体校验：未知卡片、重复卡片、超过 20 张、宽度非法一律 `422` 并回滚（不静默裁剪）——
  静默裁剪会让用户以为自己拖丢了卡片。
- 库里读到不合法的旧值（例如卡片下线后的残留）**按「没保存过」处理**，页面回落默认布局，
  不抛错、不阻塞页面。
- 版本 3 的迁移见 `app/portal/migrations.py` 的 `_v2_to_v3`：只建新表，不动既有快照数据。
  生产升级仍走 `scripts/portal_migrate.py`（`deploy.py` 重启前自动调用，备份在
  `/opt/portal-backups/<时间戳>/premigrate/`）。

## 3. 接口契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/portal/workbench` | 卡片目录 + 本人布局 + 按布局算好的卡片数据（一次请求给全） |
| PUT | `/api/portal/workbench` | 保存本人布局，body `{"cards":[{"card_id","width"}]}` |
| DELETE | `/api/portal/workbench` | 恢复默认（删除本人那一行，幂等） |

GET 响应（除统一信封 `snapshot_version / last_success_at / server_time / stale / last_error`）：

```json
{
  "has_snapshot": true,
  "catalog": [{"id": "my_tasks", "title": "我的事项", "hint": "...", "width": "full"}],
  "layout": [{"card_id": "my_tasks", "width": "full"}],
  "saved": true,
  "updated_at": 1793715454.70,
  "cards": [{"id": "my_tasks", "title": "我的事项", "hint": "...", "width": "full",
             "data": {"items": [], "total": 0}}]
}
```

- `saved=false`：这个人还没保存过布局（当前是默认布局）。
- 无首快照时返回 `has_snapshot=false`、`cards=[]`，目录照常下发（前端仍能挑卡片）。
- 单张卡的数据构造失败只在该卡上写 `error`，不影响其余卡片与整页。
- PUT/DELETE 校验 Origin/Referer（跨站 403），并写审计 `workbench_layout_save` /
  `workbench_layout_reset`（只记卡片数与提交人，不记业务数据）。

## 4. 权限边界

- 布局是「每个人自己的」：**没有飞书身份的会话（本地账号、维护者、应急会话）一律 403**，
  否则多个人会共用同一行、互相覆盖。
- 卡片数据全部走既有的成员视图与 `compute`，没有新增读取面：成员能看到什么，
  工作台上的卡片就能看到什么；卡片里出现的记录链接仍是原来的详情页。
- 这一段不引入写入能力：第一版只做「看」，改数据仍去各自页面（写入路径、队列与冲突规则不变）。

## 5. 前端交互

- 两种模式（2026-11-13 起）：默认是**功能模式**，只显示卡片本身——没有排序 / 宽度 / 移除按钮，
  也不显示卡片库，卡片区按页头实际高度现算 `min-height` 撑满一屏，进来不用滚就能看完；
  点页头右上角「配置」进**配置模式**，才露出卡片工具、底部卡片库与「恢复默认布局」，改完点「完成」收起。
- 两列网格（窄屏单列）；`half` 占一格，`full` 整行。卡片内容超出时卡内滚动，不撑破网格。
- 排序：原生 HTML5 拖放（只在配置模式下可拖）；每个卡片另有「↑ / ↓」按钮，
  键盘与触屏都能用，效果与拖拽等价。
- 增删：卡片库列出「还没放上来的卡片」，点「添加」追加到末尾；卡片上的「移除」删掉；
  「半屏 / 整屏」切宽度；「恢复默认布局」两步确认。
- 每次改动立即 PUT（不做本地暂存），成功后重新拉一遍 GET 让卡片数据与新布局对齐；
  失败时保留本地改动并显示「重试保存」，不会假装保存成功。

## 6. 新增一张卡片

1. `app/portal/workbench.py`：`CATALOG` 加一项（`id/title/width/hint/needs`），
   `_BUILDERS` 加同名构造函数，返回可序列化的 `data`；需要新数据就复用既有视图，
   不要在工作台里另开数据源。
2. `web/src/portal/pages/Workbench.vue`：模板里加一个 `v-else-if="it.card_id === '<id>'"` 分支。
3. `tests/test_workbench.py`：`CatalogTests` 的目录数量断言、`CardBuildTests` 的空快照用例会自动覆盖。
4. 本文件的目录表补一行。

## 7. 已知限制

- 前端本机已能跑 `npm run typecheck` / `npm run build`（产物入库 `static/app/`）；类型检查仍有若干
  历史基线错误（ChipFilter 的 `ariaLabel` 等），与工作台无关，改动后需确认没有新增。
- 拖拽排序不跨浏览器手势（触屏用 ↑/↓），也不支持一次拖多张。
- 布局没有「按组共享」的能力：组长想统一视图只能各自摆一遍（需要时另开需求）。
- 卡片数据每次 GET 现算：卡片多、数据量大时一次请求会变重（当前十张卡在数千条记录规模下仍是毫秒级，
  与列表页同一量级）。

## 8. 排障

| 现象 | 排查 |
| --- | --- |
| 页面提示「还没有可显示的卡片」 | 卡都被移除了：点右上角「配置」，在卡片库点「添加」，或「恢复默认布局」 |
| 保存失败提示 422 | body 里有未知卡片 / 重复卡片 / 宽度不是 half|full / 超过 20 张 |
| 保存失败提示 403 | 当前会话没有飞书身份（本地账号或应急会话），用飞书登录后再改 |
| 换设备后布局没跟过来 | 布局按飞书 open_id 存：确认两次登录是同一个飞书账号 |
| 某张卡显示「卡片数据加载失败」 | 该卡的数据源出错（其余卡正常）；看服务日志对应异常，或用 `/api/portal/sync-status` 确认快照是否 stale |
| 库版本报错（`schema_version_mismatch`） | 先跑 `scripts/portal_migrate.py`（见运维手册 §8.1） |
