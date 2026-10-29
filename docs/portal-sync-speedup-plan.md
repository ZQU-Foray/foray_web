# 门户数据同步提速规划（本地 SQL 缓存方向）

> 状态：规划稿（待评审）｜日期：调研产出｜范围：`app/portal/*` 同步/缓存/读路径
> 关联文档：`docs/portal-cache-design.md`（缓存库设计，已完成第 1–5 步）、`docs/portal-progress.md`

## 0. 摘要

结论先说：**慢的根子不是 SQLite，而是「每轮全量 HTTP 拉取 + 每轮全量重建 + 每请求全量反序列化」**。
本地 SQLite 缓存库（`store.py` + `cache_store.py`）已经把「物化结果」做完了，但没有解决三件事：

1. 同步请求数随记录数线性增长，且有一条纯展示用途的请求占了大头（记录链接 `batch_get`）；
2. 每轮同步/每次写入都把整库重新规范化、哈希、整表重写；
3. 每个 GET 请求都把整库 8 张表重新 `json.loads`（没有进程内缓存）。

按收益/成本分四层推进（数字为估算口径，见 §2）：

| 层次 | 内容 | 每轮请求数（8 表 6000 条） | 单轮耗时 | 页面 GET P50（5000 任务） |
| --- | --- | --- | --- | --- |
| 现状 | — | ≈128 次 | ≈20 s | ≈70 ms |
| P0 | page_size 500、链接差集、字段缓存、轮级退避、推送合并 | ≈20 次 | ≈4 s | ≈70 ms |
| P1 | 增量拉取（watermark）、读侧快照缓存、写模式切排队 | 稳态 ≈10 次 | ≈1.5 s | ≈5 ms |
| P2 | 本地列存 + 索引 + 分页下沉、增量 rebuild | ≈10 次 | ≈0.5 s | ≈2 ms |
| P3 | 事件订阅（可选） | ≈变更数 | ≈0.1 s | ≈2 ms |

P0/P1 改动小、可灰度、可回滚，建议先做；P2 是"本地 SQL 数据库"真正落地的部分，需要一次 schema 迁移。

## 1. 现状：数据是怎么流动的

### 1.1 读路径（GET）

```
GET /api/portal/tasks
  → auth_middleware（app/auth/guard.py:146-176，ADMIN_API_PREFIX，会话在内存）
  → api_tasks（app/portal/api.py:419）
    → _require_snapshot()（api.py:46-53）→ store.current()（app/portal/store.py:263-283）
        → 8 张表各一条 SELECT + 每行 json.loads（store.py:271-276）
        → quality 表再查一次（store.py:278-280）
    → _envelope()（api.py:29-39）→ store.status()（store.py:284-301）：6 次 meta 查询
    → 纯内存组装：_project_names(394) / _task_view(399) / 过滤排序(429-443) / compute.urgent_tasks(444)
    → _paginate（api.py:63-69，page_size 上限 200，api.py:24）
```

**没有任何进程内快照缓存**（全仓无 `lru_cache`），每个 GET 都做一次全量反序列化；同一请求里还会重复加载 2–3 次（`_my_member_record` api.py:1496、`leader_ctx` api.py:689、`_snapshot_record` api.py:1022）。

### 1.2 写路径（POST/PATCH/DELETE）

```
write_task_update（api.py:988）
  → writes.queued(table)（writes.py:52-54）
     ├─ cache 模式：cache_store.enqueue_update → changed(True) → 立即返回 loc_ 临时 ID（writes.py:70-95）
     └─ direct（默认）：FeishuWriter().update() 同步 urlopen（writer.py:30-32 每次读配置）
          → changed(False) → view.refresh() → 全量 rebuild（view.py:119-176）
```

- 改造前 `PORTAL_WRITE_MODE` 默认 `direct`（`app/config.py`）——本次 I5 起默认改 `cache`；`_must_queue = queued(table) or has_open_ops(table, record_id)`（writes.py:55-56）——direct 下已有排队操作的记录也会被强制排队（防乱序，不是 bug）。
- 直写最坏 ≈ 3 次重试 × 10 s 超时（writer.py:58-71、config.py:114），请求线程全程阻塞。
- 每次写入后 `view.refresh()` 都是**全量 rebuild**：`cache_store.table_views(8 表)` + `record_marks` + `normalize_all` + `content_digest`（全量 `json.dumps+sha256`）+ 变了才整表 DELETE/INSERT。

### 1.3 同步路径（每 60 s 一轮）

```
SyncScheduler._loop（sync.py:410-425，固定 _wake.wait(60)，失败不退避）
  → run_once（sync.py:291-350）
      → _load_sync_cfg()：每次 read_text+json.loads（sync.py:49-77）
      → 逐表（TABLE_ORDER 8 张，sync.py:30-31）：
          reader.fields(tid)        # 1 次（分页 page_size=100，sync.py:191）
          reader.records(tid)       # ceil(N/100) 次（sync.py:234 用 PORTAL_SYNC_PAGE_SIZE）
      → reader.record_links(...)    # 每表 ceil(N/100) 次 batch_get（sync.py:255-272，纯展示用）
      → view.save_sync_meta → _refresh_mirror（按行 diff upsert，cache_store.py:88-150）
      → view.rebuild(cfg, sync_round=True)：全量重建 → content_hash 未变则不发布（view.py:157-162）
```

## 2. 瓶颈量化

### 2.1 每轮同步的 HTTP 请求数（计入 8 表共 N 条记录、7 张需链接的表）

| 项 | 现行公式 | N=6000 时 | 说明 |
| --- | --- | --- | --- |
| 记录列表 | Σ ceil(N_t/100) | ≈60 | `PORTAL_SYNC_PAGE_SIZE=100`（config.py:113） |
| 字段元数据 | 8（每表 1 次） | 8 | 字段几乎不变，却每轮都拉 |
| 记录链接 | Σ ceil(N_t/100) | ≈60 | 仅用于跳飞书，`PORTAL_LINKS_ENABLED` 默认开（config.py:125） |
| 合计 | — | **≈128 次/轮** | 设 RTT 150 ms 串行 → ≈19 s，贴着 60 s 周期 |

真实规模以生产库为准，但**结构上就是线性**：记录翻倍，请求数翻倍。

### 2.2 每轮本地耗时（合成数据实测，本机）

| tasks 记录数 | `view.rebuild` | `table_views(8 表)` | `store.current()` |
| --- | --- | --- | --- |
| 500 | 20.8 ms | 4.3 ms | 6.4 ms |
| 2000 | 71.5 ms | 13.2 ms | 24.4 ms |
| 5000 | 180 ms | 38.8 ms | 66 ms |

- `table_views` 的 5000 条耗时里 `tasks` 独占 ≈30 ms，且只发 2 条 SQL——**成本在 `json.loads`，不在 N+1**（曾经的 N+1 猜测已被实测证伪：一次性取全部 open ops 仅 0.81 ms）。
- 单轮 payload 5000 条时 ≈1.9 MB：`store.current()` 一次就把这 1.9 MB 全部反序列化。

### 2.3 每请求本地耗时

`store.current()` 线性于记录数：500 条 6.4 ms、5000 条 66 ms。`/tasks` 等路由在 5000 条规模下每次请求光"读库+反序列化"就 ≥66 ms，且 `page_size` 上限只有 200——**读 200 条也要付 5000 条的代价**。

### 2.4 写入延迟

`direct` 模式：飞书 RTT（1–10 s）→ 全量 rebuild（5000 条 ≈180 ms）→ 页面刷新。这是"网页改一下要等好几秒"的直接原因。
2026-10-29 起默认 `cache`：写入只落本地队列即返回，`view.refresh` 每 20 条推送才做一次。

### 2.5 本轮改造后的实测（I1–I5 已落地，`scripts/bench_portal_sync.py` 可复现）

| tasks 记录数 | `view.rebuild` | `table_views(8 表)` | `store.current()` 首次 | 缓存命中 | `store.status()` |
| --- | --- | --- | --- | --- | --- |
| 600 | 33 ms | 8.2 ms | 13.1 ms | 0.01 ms | 0.05 ms |
| 2000 | 103 ms | 26.2 ms | 46.4 ms | 0.01 ms | 0.05 ms |
| 5000 | 288 ms | 63 ms | 104 ms | 0.01 ms | 0.05 ms |

- 稳态读路径：`store.current()` 从 66 ms → **0.01 ms**（版本号闸门缓存 + 只读连接），`status()` 从 6 次独立 meta 查询 → 单条 IN 查询 0.05 ms。
- 每轮同步的分页请求数：`PORTAL_SYNC_PAGE_SIZE` 100 → 500，5000 条任务从 50 页降到 10 页（-40 次 HTTP）；字段元数据走 6 小时缓存，稳态每轮 -8 次请求；链接补取只请求新增记录。
- **剩余成本（属于 I6/I8）**：每发布新版本后的首次读仍要 104 ms（全量反序列化 1.9 MB 快照），`view.rebuild` 仍是 288 ms 全量重建。

## 3. 优化方案

### P0-1 page_size 100 → 500（记录列表与字段元数据）

- 动机：官方文档确认列表/搜索接口 `page_size` 上限 500。
- 改动：`app/portal/sync.py:191`（字段）与 `sync.py:234`（记录）改用 `config.PORTAL_SYNC_PAGE_SIZE`；`app/config.py:113` 默认值 100 → 500。
- 收益：列表请求数 ÷5（6000 条：60 → 12）。
- 风险：单页响应体变大（5000 条 payload 约 1.9 MB→单页约 160 KB，可接受）；`MAX_PAGES=200`（sync.py:34）上界仍够。
- 验证：`tests/test_portal_sync.py` 的假上游分页用例改成兼容 500；真机看一轮请求数日志。

### P0-2 记录链接改「差集补取」

- 动机：每轮 60 次 `batch_get` 只为拿 `shared_url`（sync.py:255-272），而 99% 记录上轮已取过。
- 改动：`record_links` 只对「本地 `links` 里没有的 record_id」发请求；消失的 record_id 清理；`step` 上限仍 100（`PORTAL_LINK_BATCH`）。
- 收益：稳态链接请求 60 → 0～2。
- 风险：链接失效不会自动刷新（可接受，链接只用于展示；提供「全量刷新链接」开关）。
- 验证：`tests/e2e/test_portal_cache.py` 增加"第二轮不再请求链接"的断言。

### P0-3 字段元数据缓存

- 动机：`reader.fields()` 每轮 8 次，字段名/ID 对照几乎不变（sync.py:183-222）。
- 改动：按 `(app_token, table_id)` 缓存到 `meta` 表，带 TTL（如 6 h）与"记录拉取失败且字段仍缺"时的强制刷新兜底；字段缺失校验（sync.py:316-321）保留。
- 收益：稳态 8 → 0～8（每日 4 次刷新）。
- 风险：改字段名后最多 6 h 内可能误报"字段在源表不存在"→ 用「连续两轮失败即强制刷新字段」兜底。
- 验证：单测注入字段变更。

### P0-4 轮级指数退避

- 动机：`_loop` 失败也固定 60 s 全量重跑（sync.py:410-425），上游限流时形成风暴。
- 改动：失败计数 → `min(PORTAL_SYNC_INTERVAL × 2^n, 600 s)`；成功即清零；`trigger_now()` 仍可强制立即跑。
- 收益：限流期请求量降一个量级。
- 验证：单测 + `/api/portal/sync-status` 暴露 `next_run_in`。

### P0-5 推送侧不再逐条 rebuild

- 动机：`pusher.drain` 每条 op 后立即 `view.refresh()`（pusher.py:106-119），批量推送 100 条 = 100 次全量重建。
- 改动：`drain` 内改为"循环处理完 limit 条后 rebuild 一次"；冲突回退（回退成本地值）仍需立即重建——归并成最后一次。
- 收益：批量推送本地耗时 ÷N（5000 条规模 100 条 op：18 s → 0.2 s）。
- 风险：页面回显延迟从"每条即时"变成"整批结束"，需要 `drain` 每处理完一批（如 20 条）就重建一次折中。
- 验证：`tests/test_pusher.py` 断言 rebuild 调用次数。

### P1-1 增量拉取（本地 watermark + search 过滤）

- 动机：现行每轮拉全表；飞书 records 已带 `last_modified_time`（`automatic_fields=true`，sync.py:234-239）。
- 方案：
  1. 本地维护 `sync_state(table_name, watermark_ms)`，取已处理记录的最大 `last_modified_time`；
  2. 每轮改用 `POST /records/search` + `filter`（`LastModifiedTime isGreater <ExactDate>`）只拉变更；删除检测靠「本轮返回的 record_id 集合 vs 本地镜像」对账（search 不返回已删除记录）；
  3. 安全网：每 N 轮（如 20 轮）或每日一次全量对账，防事件丢失。
- 收益：稳态请求数 ≈ 变更页数（通常 1–2 次/表）。
- 风险（需实测确认，见 §6）：
  - 官方文档对「最后更新时间」作筛选左值只说明"日期粒度按天"，**是否支持毫秒级 ExactDate 未知**；
  - 若只到天，则降级为「按天拉取 + 本地 `last_modified_time > watermark` 过滤」（请求仍是按天分页，但只读一天的数据）。
- 验证：用一张测试表插入 1 条记录，跑 `search + isGreater`，对比返回条数与时间戳。

### P1-2 读侧：进程内快照缓存 + 只读连接 + status 合并

- 动机：`store.current()` 每请求全量反序列化（store.py:263-283），且 `_envelope` 每次 6 次 meta（store.py:284-301）。
- 改动：
  1. `store.current()` 按 `current_version` 缓存解析结果（模块级 dict，发布新版本时失效）；
  2. `status()` 的 6 次 meta 合并成 1 次 `SELECT key,value FROM meta WHERE key IN (...)`；
  3. 读侧独立 `check_same_thread=False` 只读连接（WAL 下并发读），不抢 `_lock`（store.py:28）。
- 收益：5000 任务规模 GET P50 从 ≈70 ms 降到 <5 ms；同请求重复加载归零。
- 风险：多进程部署时缓存不共享——本项目是单进程 uvicorn 部署（运维手册），可接受；若将来多 worker，用版本号做 key 即可自愈。
- 验证：`tests/test_store.py` + 基准脚本复跑。

### P1-3 写模式：direct → 排队（cache 模式）

- 动机：直写把飞书 RTT 挂在请求线程上（§2.4），而排队模式基础设施已完整（outbox + pusher + 冲突 + 回退）。
- 改动：`PORTAL_CACHE_TABLES`（config.py:134）扩到全部可写表；`PORTAL_WRITE_MODE` 默认改 `queue`；采购附件仍走直写（`writes.py` 附件例外）。
- 收益：写请求 P50 从秒级降到 <100 ms，飞书故障不再阻塞页面。
- 风险：冲突率上升（多人同时改同一记录会被检测为冲突并按飞书为准）；需要前端把"待同步/冲突"提示（已有 `view.sync_mark`）展示到位。
- 验证：`tests/e2e/test_portal_cache.py` 全绿；手工验证"断网写 → 队列积压 → 恢复后自动推送"。

### P2-1 本地列存：`records` 表 + 索引 + 分页下沉

- 动机：JSON blob 快照必须整库反序列化才能过滤/分页（§2.3）。
- 方案：新增 `records(table_name, record_id, name, project_id, group_name, status, priority, owner_ids, planned_at, modified_at, data_json)` + 索引（`table_name`、`project_id`、`status`、`modified_at`）；API 的分页/排序/简单过滤改为 SQL；复杂组装仍用 Python 但只读本页记录。
- 收益：GET 从 O(全库) 降到 O(页)；`/overview` 类聚合可下沉 `GROUP BY`。
- 风险：需要一次迁移（v2 → v3）与双写期；`tests/e2e/run_all.py` 全量回归。
- 验证：分页/排序结果与旧快照逐条比对（迁移脚本自带对账模式）。

### P2-2 增量 rebuild

- 动机：`view.rebuild` 是全量（view.py:119-176），5000 条 180 ms。
- 改动：按"受影响表"重建（同步返回哪些表有新数据）；进一步按受影响记录局部 upsert（需要 P2-1 的列存）。
- 收益：单轮本地耗时 180 ms → 10–30 ms（只改 1 张表时）。
- 风险：`content_digest` 全量哈希仍在，需改为按表哈希再合并。

### P3 事件订阅（可选，终局）

- 飞书 `drive.file.bitable_record_changed_v1` 事件支持 Webhook / 长连接，事件体含 `record_id`/`action`/`before_value`/`after_value`/`revision`/`update_time`（秒级）。
- 事件 → 只取该记录（`batch_get`）→ 本地 upsert → 只重建受影响表。
- 代价：需申请权限（`bitable:app` 或 `drive:drive`）+ 订阅云文档事件 + 常驻连接；公式/查找引用字段变化**不触发**事件，仍需轮询兜底。
- 建议：P1–P2 落地后按实际需要评估，不作为一期目标。

## 4. 交付阶段与任务拆分

| 阶段 | 内容 | 预计 | 可独立上线 |
| --- | --- | --- | --- |
| A（P0） | page_size、链接差集、字段缓存、轮级退避、推送合并 | 1–2 天 | 是 |
| B（P1） | 增量拉取、读侧缓存、写模式切排队 | 2–3 天 | 是（可分三项） |
| C（P2） | 列存迁移 + 分页下沉 + 增量 rebuild | 3–5 天 | 是 |
| D（P3） | 事件订阅 | 视需要 | — |

每阶段独立可验收，避免"大爆炸"改造；每阶段保留 `PORTAL_*` 开关用于回滚。

## 5. 验收标准（可测）

1. 一轮同步的 HTTP 请求数：P0 后 ≤ 20，P1 稳态 ≤ 10（日志打点 `_run_once` 计数）。
2. 5000 任务规模：`GET /api/portal/tasks` P50 < 10 ms（P1）/ < 5 ms（P2）（pytest-benchmark 或手工 100 次平均）。
3. 写请求 P50 < 200 ms（P1-3 后）。
4. 页面"改完立即看到"：从发起写入到 `/api/portal/tasks` 返回新值 < 200 ms。
5. 同步失败退避：连续失败 3 轮后间隔 ≥ 240 s；恢复后 1 轮内回到 60 s。
6. `tests/e2e/run_all.py` 全绿；新增用例覆盖上述各项。

## 6. 待验证事实（需要真实凭据与生产表）

1. `POST /records/search` 的 `filter` 对「最后更新时间」是否支持毫秒级 `isGreater`（官方文档只保证"日期粒度按天"）。
2. 生产各表真实记录数与每轮实际请求数（当前仓库无 `config/portal_sync.json`、无 `config/portal_snapshot.db`，无法实测）。
3. `page_size=500` 时飞书单页响应体与耗时是否稳定（限流 20 次/s 下）。
4. 生产是否单进程 uvicorn（决定读侧进程内缓存是否成立）。

## 7. 风险与回滚

| 风险 | 影响 | 回滚手段 |
| --- | --- | --- |
| page_size 500 触发上游限流 | 同步失败 | `PORTAL_SYNC_PAGE_SIZE=100` |
| 链接差集漏更新 | 链接过期 | `PORTAL_LINKS_REFRESH=all` 全量重取 |
| 字段缓存过期 | 误报配置错误 | 连续两轮失败自动强制刷新 |
| 增量拉取漏记录 | 数据缺失 | 关闭增量（`PORTAL_SYNC_INCREMENTAL=0`）回到全量 + 每日对账 |
| 写模式切排队 | 冲突提示增多 | `PORTAL_WRITE_MODE=direct` |
| 列存迁移失败 | 读不到数据 | 迁移脚本 `--rollback` 恢复 v2 快照（备份文件） |
