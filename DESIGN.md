# DeepAggregator 设计文档（v1.2）

> 状态：**基线随实现滚动更新（2026-09-29，v1.2）**。本文档是实现的唯一依据；实现与文档冲突时，以修改后的文档为准。变更摘要见 §12 / §13。
> 进行中：**源插件平台重构**（目标/契约决策/阶段计划/验收标准见 PLUGIN_PLATFORM_PLAN.md；基线快照 = git 标签 `v1.2-baseline`），各阶段落地后由本文档滚动吸收。

## 1. 项目定位

一站式私人信息终端：像添加 RSS 源一样添加"订阅"，订阅源可以是真正的 RSS，也可以是一个爬取目标（当前支持 RSS + arXiv + Reddit）。所有入库信息分两级存储——**摘要（热数据，结构化）+ 原文（冷数据，按需归档的二进制/文档）**，通过 Web UI 完成阅读、检索（关键词/语义/混合）、原文归档与页内重现，并支持多工作区隔离（如"学术区 / 游戏区"）。

**非目标**：通用网页爬取（scrape 适配器，v2）、跨工作区并行抓取、多用户、高可用。

## 2. 架构决策记录（ADR 摘要）

| # | 决策 | 结论 | 关键理由 / 放弃的替代方案 |
|---|------|------|--------------------------|
| 1 | 数据库 | **SQLite（WAL + FTS5 trigram）** | 个人规模单文件即备份；放弃 Postgres/MinIO（多余运维）、DuckDB（OLTP 弱） |
| 2 | 向量执行 | **sqlite-vec 为唯一后端**（`vec0` 虚表 + KNN） | 与 SQLite 同文件、零服务；放弃 numpy 暴力、LanceDB/Chroma（第二存储引擎）、pgvector（留作量大后的迁移终点） |
| 3 | Embedding | **本地 ONNX 版 Qwen3-Embedding-0.6B（INT8，烘焙进镜像 /model，onnxruntime CPU 推理）为默认；OpenAI 兼容 API 为备选，设置页切换** | 一键离线部署、镜像内无 torch；ONNX 权重取自社区导出 `n24q02m/Qwen3-Embedding-0.6B-ONNX`（Apache-2.0）。模型标识服务端强校验必须为 Qwen3-Embedding-0.6B |
| 4 | 部署形态 | **单镜像模块化单体** | 需要的是代码边界而非部署拆分；`core`/`web` 包间严格隔离，边界可将来升级为进程/网络边界 |
| 5 | 换 embedding 模型 | 向量带 `model`+`dim` 标签隔离；**换模型/dim = 清空向量表并自动全量重嵌**（向量为可再生派生数据）；**同模型换计算设备（cpu/directml/cuda）热切换无感** | 不做跨模型比较；检索在迁移期短暂降级可接受 |
| 6 | 源插件架构 | **每个 source = 一个自包含模块，实现三能力：`fetch()` 爬取器 + `archive_requests()` 存档策略（有序请求列表，首个成功即止）+ `viewer()` 原文视图（含回落链）**，注册表 `SOURCES = {type: class}` | 爬取/存档/显示高度特化且必须内聚（用户明确要求）；扩展新源 = 新增一个文件 + 注册一行。放弃"通用爬虫 + 全局配置"方案 |
| 7 | 归档命名 | **源官方命名优先**（arXiv 用论文编号 `2609.31616v1.pdf`；RSS 从 URL slug 派生，真撞名才加 `-2` 后缀），目录按源类型分桶 `assets/<type>/`；**sha256 仅作数据库完整性字段，不参与命名** | 内容哈希命名不可读（用户明确否决）；arXiv 编号天然防碰撞，同名重归档 = 幂等覆盖 |
| 8 | 归档覆盖语义 | **重归档 = 覆盖**：成功后清理该条目的旧资产行与不再被引用的旧文件；失败保留旧档（状态 failed） | 避免"每次点存原文多一行重复资产"；失败不破坏既有归档 |
| 9 | 任务调度 | 进程内 asyncio 循环（抓取扫描 60s / 向量补齐轮询 / 归档即发即忘），**抓取失败逐源指数退避（5→10→20→30 分钟封顶，按 工作区+源 隔离计数）**，源间 3 秒礼貌间隔 | 不引入 APScheduler/Celery；对 arXiv 406 边缘限流的自愈能力是刚需 |
| 10 | 多工作区 | **`DA_DATA_DIR` 语义为工作区根目录，卷内每子目录 = 独立工作区（各自的 db + assets）；单活跃语义，运行时经 API 切换，`.current` 指针记住上次活跃** | volume 从日常操作中隐退；并行多工作区留待多容器方案 |
| 11 | 运行时设置 | **`<数据根>/settings.json` 为运行时设置存储（ai / embedding / reddit 三段），`DA_*` 环境变量仅作初始默认值；设置页保存后落盘并即时热生效**（embedding 变更 = 重建 embedder 实例，无需重启） | 环境变量改一次要重启容器，不适合"AI 服务商随手切换"；设置文件随数据卷备份 |
| 12 | Reddit 接入 | **OAuth `client_credentials`（应用级只读授权，无需账号密码）**，token 进程内缓存；抓取 = `/r/sub1+sub2/new`；存档 = 帖子 + top20 热门评论 JSON 优先（`?limit=20&sort=hot`）、HTML 回落；显示 = 前端渲染 reddit 风格帖文树（srcdoc），**视频（v.redd.it mp4）/YouTube 链接帖（官方 iframe 播放器）/图集/图片内嵌直接查看，无媒体才回落纯链接**，失败回落 iframe | 只读公开帖无需 password grant；JSON 是 Reddit 数据的完整形态，HTML 是降级品；内嵌媒体让用户无需跳转原帖即可完成查看（mp4 无独立音轨，页内注明并附原帖链接） |
| 13 | AI Markdown 存档 | **逐源开关 `archive_markdown`**：HTML 下载成功后抽正文文本 → OpenAI 兼容 chat（读 ai 设置段）生成纯净 Markdown → 存 `.md`；AI 失败/未配置自动回落保存原始 HTML | Unity blog 等 SPA 源的原始 HTML 基本不可读（用户原始痛点）；LLM 失败必须有降级路径 |
| 14 | SQLite 于绑定挂载 | 加固三件套：`busy_timeout`、**SELECT 游标必须全量消费**（`one()` 内部 fetchall；未耗尽的语句会锁 schema 导致后续 DDL `SQLITE_LOCKED`）、**表结构重建迁移用单事务（BEGIN…COMMIT）+ 外键临时关闭 + `DROP TABLE IF EXISTS` 幂等** | Windows 绑定挂载 + 崩溃中断曾造成 "table sources_new already exists" 崩溃循环 |
| 15 | 前端状态机 | **Feed 控制器**：`state.feed = {sourceId, q, mode, unread, starred}` 为唯一状态源；所有控件（侧栏源点击、搜索框、筛选器）只做"翻译成 `setFeed()`"一件事，由控制器统一回写控件并触发加载 | 曾出现侧栏选择与"全部来源"下拉互相矛盾（搜索被残留源过滤）；任何未来控件都接同一机制 |
| 16 | 前端热更开发模式 | 容器挂载 `app/web/static`（前端刷新即生效）与 `app/`（后端 `docker restart` 生效）；**依赖变更才需要重建镜像** | 迭代期免构建；正式发布仍以镜像为准 |
| 17 | 存档任务队列 | **统一 `ArchiveQueue`：同一 (工作区, 源) 内 FIFO 串行、跨源并行；任务绑定入队时的工作区 Database（热切换不影响执行）；所有状态变化经进程内 EventBus → SSE `/api/events` 广播**；启动时自动重排队列中断遗留的 pending 条目 | 旧实现"即发即忘"导致快速连点多条只有最后一条可见进度、卡片状态靠轮询且不同步；用户明确要求"依次点击、按源排队、广播更新"；放弃轮询方案（延迟高、浪费请求） |
| 18 | 开发迭代范式 | **日常迭代围绕常驻容器 `deepaggregator`**（Docker Hub 镜像 + `app/`、`workspace/` 双 bind mount，端口自定义映射如 `57577:8080`，restart=unless-stopped）：前端改动 → 用户 Ctrl+F5 即生效；后端改动 → 改动方执行 `docker restart deepaggregator`（优雅 SIGTERM，走 lifespan 清理）后告知用户刷新；**迭代期禁止 `compose up` / 重建镜像**；**容器运行期间严禁第二个进程（本地 uvicorn、sqlite3 CLI 等）访问 `workspace/` 下的任何 `.db` 文件** | 镜像已发布 Docker Hub，重建/compose 迭代太慢（用户明确要求）；跨 OS（Windows 宿主进程 + 容器 VM 进程）并发访问 bind mount 上的 SQLite 会触发容器内 `disk I/O error` 与已提交写入回退——2026-09-28 实际事故，ADR 14 的加强版教训 |
| 19 | YouTube 源与上传存档 | **YouTube = 第四种源**：频道 RSS 抓取（`feeds/videos.xml?channel_id=`，免 API key；@handle 需抓一次频道页解析 UC id，进程内缓存），条目带缩略图（`items.image`），feed 卡片内嵌封面图；**视频本体不走自动存档**（watch 页快照仅兜底），新增逐源开关 `allow_upload`（默认关）：开启后详情页出现「上传存档」按钮，用户把外部工具下载好的视频等文件上传进系统，**覆盖语义与 ADR 8 一致**；viewer 优先上传的 video/image 资产 | YouTube 视频无法在容器内直接下载（用户明确接受外部工具下载 + 上传的分工），系统由此承担"分类保存"职责；频道 RSS 是唯一免凭据的官方渠道；上传门禁按源控制，避免误传污染其它源 |

## 3. 总体架构

```
浏览器（手机/PC）
   │ HTTP（唯一对外端口，默认 8080）
┌──▼────────────────────────────────────────────────┐
│ 单容器 · 单进程（uvicorn）                          │
│                                                   │
│  app/web/   交互层：鉴权(Bearer) → FastAPI 路由     │
│             → 静态 SPA（原生 JS + Feed 控制器）      │
│                │ 仅经 CoreAPI 方法调用（进程内边界）  │
│  app/core/  数据引擎（不 import fastapi）：          │
│    sources(rss/arxiv/reddit) · ingest · embeddings │
│    search · fulltext · ai · appsettings            │
│    jobs(抓取/向量/归档 workers) · workspaces        │
│                │                                   │
│  <工作区>/aggregator.db（摘要+FTS5+vec0+元数据）     │
│  <工作区>/assets/<源类型>/<官方命名>（原文文件）      │
│  /data/settings.json（运行时设置，跨工作区共享）      │
└───────────────────────────────────────────────────┘
```

**边界纪律**：`app/core/` 不 import fastapi；`app/web/` 不 import sqlite3 / 不直接读写库。一切数据访问经 `CoreAPI`。

## 4. 数据模型

```sql
-- 订阅源（每工作区一张）
CREATE TABLE sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK(type IN ('rss','arxiv','reddit','youtube','bilibili')),
  name TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',            -- rss: feed 地址; arxiv: API base(默认官方); reddit: 空; youtube: 频道值(展示用); bilibili: 空间值(展示用)
  config_json TEXT NOT NULL DEFAULT '{}',  -- arxiv: {"categories":[..],"max_results":50}
                                           -- reddit: {"subreddits":["localllama","unity"],"limit":25}
                                           -- youtube: {"channel":"@handle / URL / UC id"}
                                           -- bilibili: {"channel":"https://space.bilibili.com/<mid> / mid"}
  fetch_interval_min INTEGER NOT NULL DEFAULT 60,
  enabled INTEGER NOT NULL DEFAULT 1,
  color TEXT NOT NULL DEFAULT '',          -- 源专属颜色（空=主题紫），用于卡片标签/侧栏圆点/未读高亮基调
  archive_enabled INTEGER NOT NULL DEFAULT 1,   -- 是否允许"存原文"
  archive_markdown INTEGER NOT NULL DEFAULT 0,  -- HTML 存档改用 AI 生成纯净 Markdown（需 ai 设置段已配置）
  allow_upload INTEGER NOT NULL DEFAULT 0,      -- 允许"上传存档"（用户上传本地文件覆盖存档，默认关）
  sort_order INTEGER NOT NULL DEFAULT 0,        -- 组内显示顺序（拖拽排序落库）
  last_fetched_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 工作区级 UI 偏好（如源分组顺序，值为 JSON）
CREATE TABLE ui_prefs(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 摘要表（每条 ≤0.5KB 级）
CREATE TABLE items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  guid_hash TEXT NOT NULL UNIQUE,          -- 去重锚点 sha256(guid|url)
  guid TEXT NOT NULL DEFAULT '',           -- 原始 guid（如 reddit t3_xxx / youtube yt_<videoId>），供存档策略提取资源 id
  title/summary/url/author/image/published_at/fetched_at/is_read/is_starred ...,  -- image=封面图 URL，feed 卡片内嵌
  fulltext_state TEXT NOT NULL DEFAULT 'none'
    CHECK(fulltext_state IN ('none','pending','done','failed'))
);
-- FTS5 trigram 虚表 + 触发器同步（略，同 v1）

-- 向量：vec0 虚表惰性创建；vector_meta 带 model/dim 标签（略，同 v1）

-- 原文归档索引（二进制本体在文件系统，路径 = assets/<源类型>/<官方命名>）
CREATE TABLE assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                      -- pdf/html/md/json/image/video/file
  mime TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,                    -- 完整性校验用，不参与命名
  path TEXT NOT NULL,                      -- 相对 assets/，形如 "arxiv/2609.31616v1.pdf"
  name TEXT NOT NULL DEFAULT '',           -- 展示用文件名
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

迁移策略：全部向后兼容式 `ALTER TABLE ADD COLUMN`（color/archive_*/guid/name）；不兼容变更（如 sources 的 CHECK 扩容）用**单事务表重建**（BEGIN → DROP IF EXISTS 暂存表 → 建新表 → INSERT SELECT → DROP 旧表 → RENAME；期间 `PRAGMA foreign_keys=OFF`，源 id 原样保留故无孤儿）。

容量模型不变：摘要 0.5KB/条；向量 4KB/条；增长主力是 assets/，单文件上限 200MB。

## 5. 核心流程

- **工作区**：`WorkspaceManager` 以数据根目录子目录扫描为注册表；API 创建/切换（原子改写 `.current`），抓取/向量化循环每轮解析当前工作区，热切换无需重启；旧单库布局自动迁移至 `default/`。

- **采集**：抓取扫描循环（60s）找到期源（失败源按指数退避跳过）→ 源模块 `fetch()` 归一化为 `RawItem` → `guid_hash` 去重入库（同时保存原始 guid 与封面图 image）→ 更新 `last_fetched_at`。RSS=feedparser；arXiv=官方 API；Reddit=OAuth token（缓存 1h）+ `/r/a+b/new`；YouTube=频道 RSS（@handle 先解析 UC id，进程内缓存）。
- **向量化**：补齐循环扫描"无当前 model 向量"的条目 → 逐条 ONNX 推理（静态 batch=1）→ 写 vec0 + vector_meta；provider/设备变更后自动使用新 embedder 实例。
- **检索**：关键词 = FTS5 MATCH；语义 = 查询向量 KNN 按 model 过滤；混合 = RRF(k=60)。前端 Feed 控制器统一驱动。
- **归档**：UI 触发（源禁用 `archive_enabled` 时隐藏按钮）→ `ArchiveQueue.enqueue`（条目立即落 `pending`，同源 FIFO 排队、跨源并行）→ `execute_archive`：源模块 `archive_requests()` 给出有序请求列表（可含认证头）→ 执行器逐个尝试、**首个成功即止**：下载（流式，≤200MB）→ 明确 kind 校验 → （若源开 `archive_markdown` 且 kind=html：抽正文 → LLM 生成 Markdown → 存 `.md`，失败静默回落原始 HTML）→ 官方命名写入 `assets/<type>/` → 清理旧资产行与孤儿文件 → `done`；全部失败 → `failed`（原始 URL 始终保留）。queued/started/progress/done/failed 全程经 EventBus 广播。已存档条目可 `DELETE /api/items/{id}/archive` 删除（文件+行+状态复位，任务进行中返回 409）。调试归档失败先看 `docker logs` 中 `deepaggregator.fulltext`/`archive_queue` 的 WARNING。
- **设置**：`GET/PUT /api/settings`；`AppSettings.load()` 以 env 默认值合并已存 settings.json（load 与 update 的 section 列表已收敛为单一常量 `SECTIONS = (ai, embedding, reddit, bilibili, translate)` 共用——曾因 load 漏 reddit 导致凭据重启即丢，审计抓出；后经两轮扩容（bilibili/translate）靠人手同步两处元组，现已机制化）；PUT 后 `CoreAPI.apply_settings()` 重建 embedder 热生效。

## 6. 模块划分

```
app/
├── config.py          # pydantic-settings，环境变量前缀 DA_（运行时设置的初始默认值）
├── main.py            # 组装：WorkspaceManager/AppSettings/Embedder/CoreAPI/JobManager → FastAPI(lifespan)
├── core/              # 数据引擎（无 fastapi 依赖）
│   ├── db.py          #   连接(线程本地)/schema/向后兼容迁移/原子表重建
│   ├── api.py         #   CoreAPI：core 唯一公开接口（含 apply_settings 热切换）
│   ├── appsettings.py #   AppSettings：settings.json 读写 + env 合并（load/update 段一致性是关键不变量）
│   ├── workspaces.py  #   WorkspaceManager（注册表/指针/统计）
│   ├── sources/       #   ★ 源插件（三能力：fetch + archive_requests + viewer）
│   │   ├── base.py    #     RawItem(含 image 封面)/ArchiveRequest(可带认证头)/BaseSource/USER_AGENT
│   │   ├── rss.py     #     feedparser；存档/视图走基类默认（HTML + 回落链）
│   │   ├── arxiv.py   #     官方 API；存档 PDF 优先 + abs 页 HTML 兜底；视图 PDF 内嵌
│   │   ├── reddit.py  #     OAuth client_credentials(token 缓存)；JSON 优先；帖文树视图数据
│   │   ├── youtube.py #     频道 RSS（@handle 解析 UC id + 缓存）；封面图；视频走用户上传，viewer 优先 video/image
│   │   └── bilibili.py#     空间视频接口（wbi 签名 + buvid + SESSDATA 免 -352）；封面/时长列表自带；POP 走文档级画中画
│   └── services/      #   ingest / embeddings(Onnx+OpenAICompat+build_embedder) / search / source_health
│   │                  #   fulltext(execute_archive+markdown 分支) / archive_queue(ArchiveQueue+
│   │                  #   EventBus，按源串行+SSE 广播) / ai(LLM markdown) / jobs(退避调度)
└── web/               # 交互层（无 sqlite 依赖）
    ├── routes.py      #   /api/* 路由 + Bearer 鉴权
    └── static/        #   index.html / app.js(Feed 控制器+设置面板+Reddit 渲染器) / style.css
tests/                 # pytest 55 项：五源模块（解析/存档计划/viewer 回落/频道 id 与 mid 解析/wbi 签名/时长回填）、
                       # 入库去重与封面、存档队列（串行/去重/事件/恢复/删除守卫）、上传存档（覆盖/门禁/超限）、
                       # 三模式检索与类型/多源/书签筛选、源排序偏好、凭据问题登记、appsettings 合并、
                       # html_to_text、工作区隔离与迁移
```

**扩展新源的成本** = 新增 `sources/<type>.py` 实现三能力 + `SOURCES` 注册表加一行 + （可选）设置页加凭据段。

## 7. Web API（全部前缀 /api，鉴权后）

| 方法/路径 | 说明 |
|---|---|
| GET `/workspaces` · POST `/workspaces` · POST `/workspaces/{name}/activate` | 工作区列表/创建/热切换（同 v1） |
| GET/POST `/sources` · PATCH/DELETE `/sources/{id}` | 源 CRUD（类型含 youtube）；PATCH 支持名称/地址/间隔/启停/颜色/archive_enabled/archive_markdown/allow_upload/config |
| GET/PUT `/source-prefs` | 源列表 UI 偏好：分组顺序 + 组内源顺序（拖拽排序落库到 ui_prefs / sources.sort_order） |
| POST `/sources/{id}/refresh` | 立即抓取一次 |
| GET `/items` · GET `/items/{id}` · PATCH `/items/{id}` | 信息流（q/mode/source_id/**source_ids**/type/**types**/unread/starred/**bookmarked**——source_ids 与 types 为 CSV 多选、同现按 OR 并集；mode 默认 **semantic**）/ 详情（含 viewer 决策）/ 已读·星标·书签 |
| POST `/items/mark-read` | **批量已读**：body `{source_id}` 或 `{type}`（侧栏长按充能触发），返回 `{marked}`；路由需保持显式 POST（与 GET `/items/{id}` 靠方法区分） |
| POST `/items/{id}/archive` | 触发归档（入队，同条目重复入队拒绝；源禁用存档时拒绝） |
| POST `/items/{id}/archive/upload` | 上传存档：请求体为**原始文件字节**、`?filename=` 传文件名（刻意不用 multipart，免 python-multipart 依赖），作为该条目存档（覆盖语义）；需源 `allow_upload=1`，否则 403；进行中 409；超限 413 |
| DELETE `/items/{id}/archive` | 删除已存档原文（文件+资产行+状态复位；队列中/执行中返回 409） |
| GET `/archive/queue` | 当前工作区存档队列快照（排队/执行中 + 本次运行历史） |
| GET `/credential-issues` | 源凭据问题登记（类型/受影响源/错误摘要），顶栏 ❌ 按钮数据源；保存凭据或抓取成功自动清除 |
| POST `/bilibili/login/qr` | 生成 B站扫码登录二维码（passport 官方接口） |
| GET `/bilibili/login/qr/poll` | 轮询扫码状态（86101 未扫 / 86090 待确认 / 0 成功自动落库凭据并清除对应问题登记） |
| GET `/stats` | 条目/未读/源/向量/资产/embedder 统计 |
| GET `/events` | SSE 事件流（`archive.queued/started/progress/done/failed/deleted`；EventSource 无法带 Header，支持 `?token=` 鉴权） |
| GET `/assets/{id}/file` | 取归档文件；`Content-Disposition: inline` + 原始文件名（页内打开而非下载）；路径校验防穿越 |
| GET/PUT `/settings` | 运行时设置视图 / 部分更新（embedding 模型强校验 Qwen3-Embedding-0.6B；PUT 即热应用） |
| **AI 对话 `/chats`** |||
| GET/POST `/chats` · PATCH/DELETE `/chats/{id}` | 会话列表（含 pinned/archived）/ 新建（自动标题）/ 重命名·置顶·归档 / 删除（级联消息） |
| POST `/chats/{id}/clone` | 克隆会话（标题加「（副本）」） |
| GET `/chats/{id}/messages` · PATCH/DELETE `/chats/{id}/messages/{mid}` | 消息列表（含 reasoning 思维链解析）/ 编辑（可 `truncate_after` 截断重发）/ 删除（用户消息连带其回复） |
| PUT `/chats/{id}/messages/{mid}/rating` | 消息评分（1/-1/0） |
| GET `/chats/{id}/context-items` · GET `/chats/for-item/{item_id}` | 会话关联条目反查（双向） |
| GET `/chats/list-context` | **列表 context 组装**（复用 search_items 全套过滤，`# 标题
摘要` 格式，免疫浏览器翻译） |
| GET `/chats/context-preview` · POST 同路径 | context 详情预览逐块提取 / 测试 |
| POST `/chats/{id}/chat` | **SSE 流式对话**（delta/reasoning 思维链/message_id/title 事件；支持 regenerate/continuation；PDF 图表页走视觉通道） |
| **AI 媒体与翻译** |||
| POST `/ai-media/upload?filename=` · GET `/ai-media/{name}` | 聊天图片上传（**原始字节流**，免 multipart）/ 读取（inline） |
| POST `/translate` · POST `/translate/test` | 翻译（三档 provider：none 拒绝 / cloud google+MyMemory / 自定义 OpenAI 兼容 API）与设置页测试连接 |
| GET `/stats` | 条目/未读/源/向量/资产/embedder 统计 |

## 8. 配置

**运行时设置（settings.json，设置页编辑，热生效）**：

| 段 | 字段 | 说明 |
|---|---|---|
| `ai` | provider/base_url/api_key/model | OpenAI 兼容端点预设（DeepSeek/OpenRouter/本地/自定义）；供 AI Markdown 与 v2 摘要使用 |
| `embedding` | provider(local/api)/model/local_path/device(cpu/directml/cuda)/api_base/api_key | 模型锁定 Qwen3-Embedding-0.6B；非 CPU 设备需对应 onnxruntime 变体，不可用自动回落 CPU |
| `reddit` | client_id/client_secret | reddit.com/prefs/apps 的应用凭据，client_credentials 只读授权 |
| `bilibili` | sessdata | B站登录态 Cookie（SESSDATA，浏览器 F12 复制）：空间抓取必需，规避 -352 风控 |

**环境变量（`DA_` 前缀，仅作首次启动默认值）**：`DA_DATA_DIR`（工作区根，compose 挂 `./workspace:/data`）、`DA_WORKSPACE`、`DA_HOST/DA_PORT`、`DA_AUTH_TOKEN`、`DA_EMBEDDING_*`（provider/model/local_path/hub_id/max_length/device/api_base/api_key）、`DA_EMBED_BATCH_SIZE/DA_EMBED_POLL_SECONDS`、`HF_ENDPOINT`（国内 `https://hf-mirror.com`）。

不变量：**`AppSettings.load()` 与 `update()` 的 section 元组必须一致**（当前均为 ai/embedding/reddit）——新增设置段时两处同步改，否则"存得进、读不回"。

## 9. 部署与运维

- `docker compose up -d`（镜像 `zsydeepsky/deepaggregator:latest`，数据卷 `./workspace:/data`，`restart: unless-stopped`；公网必设 `DA_AUTH_TOKEN`）
- **开发迭代（ADR 18，日常唯一工作方式）**：常驻容器 `deepaggregator` 已挂 `app/`（→ `/app/app`）与 `workspace/`（→ `/data`），端口自定义映射（如 `57577:8080`）。循环 = 改代码 → 前端改动用户 Ctrl+F5；后端改动执行 `docker restart deepaggregator` 后告知用户刷新 → 验证走 `http://localhost:57577/api/*`。**迭代期不 compose、不重建镜像**（仅依赖变更或功能整体完善发布时才做）；**容器运行时严禁宿主机第二个进程访问 `workspace/` 下的 `.db` 文件**。若容器内出现 SQLite `disk I/O error`：`docker stop deepaggregator` → 宿主侧对每个 `workspace/*/aggregator.db` 执行 `PRAGMA integrity_check` + `PRAGMA wal_checkpoint(TRUNCATE)` → `docker start deepaggregator`
- **备份 = 拷贝 `workspace/` 目录**（含所有工作区 db + assets + settings.json）
- **发布双通道**：`build_image.ps1 [-Push]` → 模型烘焙（宿主 `model-cache/` → COPY 进镜像 `/model`，构建期不联网，pip 走清华源）→ `docker_image/` 离线 tar.gz（双标签：latest + 时间戳）和/或 Docker Hub 推送（层去重）；目标主机 `docker load` 或直接 `compose up -d` 拉取
- 嵌入式模型随镜像分发；本地开发首次向量化自动从 HF 镜像源下载 INT8 模型，或 `DA_EMBEDDING_LOCAL_PATH=./model-cache` 复用
- 迁移表结构变更必须走 §4 的原子重建模式；调试归档失败先看 `docker logs` 中 `deepaggregator.fulltext` / `deepaggregator.archive_queue` 的 WARNING

## 10. 测试策略

- 源模块：三源解析（RSS/Atom/Reddit children）、存档计划（arXiv PDF 优先、Reddit JSON 优先 + 认证头）、viewer 回落链、subreddit 归一化（离线，token 用缓存桩）
- 入库：去重幂等；检索：FakeEmbedder 驱动语义/混合 + FTS5 关键词；appsettings 合并与持久化；html_to_text 剥离 script/style；工作区隔离与迁移
- 存档队列：同源 FIFO 串行/跨源并行、同条目去重、事件序列（queued→started→done）、pending 启动恢复、delete_archive 清文件与 409 守卫（假执行器驱动）
- 冒烟：uvicorn 启动 → sources CRUD → items 列表 → 归档状态机 → 静态页可达

## 11. v2 路线图（占位，未排期）

通用 scrape 适配器（选择器规则 + 限速/robots）→ 通用 LLM 摘要器（对无原生摘要的源，ai 设置段已就绪）→ trafilatura 正则级正文提取（替代 html_to_text）→ 归档失败重试队列 → 通知推送（webhook/bark）→ OPML 导入导出 → 服务端可取消的查询任务化。

## 12. v1.1 变更摘要（相对 v1 定稿）

1. **源插件架构**：`adapters/` 重构为 `sources/`，三能力内聚（ADR 6/7/8）；新增 Reddit 源（ADR 12）
2. **AI Markdown 存档**：逐源开关 + 失败回落（ADR 13）；assets kind 扩展 md/json
3. **设置中心**：settings.json 运行时设置 + 左 Tab 右详情面板；添加订阅迁入设置页；embedding 可配 provider/设备并热切换（ADR 3/5/11）
4. **存档命名与覆盖语义**：官方命名替代哈希命名（ADR 7/8）
5. **数据库加固**：游标锁/原子迁移/幂等重建（ADR 14）；items 增 guid 列
6. **前端**：Feed 控制器（ADR 15）、搜索匹配度指示器 + 查询遮罩/取消、未读高亮统一、点开即已读、星标双向同步、源颜色系统、固定侧栏/搜索栏、顶栏锁高、主题化滚动条、归档状态轮询后按钮原地出现
7. **调度**：抓取失败指数退避（ADR 9）
8. **部署**：compose 卷统一 `./workspace`；开发模式挂载 `app/`（ADR 16）

## 13. v1.2 变更摘要（2026-09-28）

1. **统一存档任务队列**（ADR 17）：新增 `services/archive_queue.py`（ArchiveQueue + EventBus），同 (工作区, 源) FIFO 串行、跨源并行、同条目去重；任务绑定入队时的工作区 Database，热切换不串库；启动时自动恢复中断遗留的 pending 条目；`fulltext.archive_item` 重构为可回调进度的 `execute_archive`（抛 `ArchiveError`、返回归档结果）
2. **SSE 事件广播**：`GET /api/events` 推送 `archive.queued/started/progress/done/failed/deleted`；鉴权支持 `?token=`（EventSource 不带 Header）；前端一次订阅，卡片徽标/详情面板/顶栏/队列面板统一由事件驱动更新，移除详情页 3s 轮询——消除"存档完成后卡片不同步"的缺陷
3. **队列 UI**：顶栏设置左侧新增队列指示器（spinner + 完成数/总数）；点击展开队列面板，任务按源分组显示排队中/下载中(第 x/y 个请求)/已完成/失败；页面加载与 SSE 重连时拉取 `/api/archive/queue` 快照对齐
4. **删除存档**：`DELETE /api/items/{id}/archive` + 详情页"删除存档"按钮（`confirm` 二次确认）；已归档条目隐藏"存原文"，任务进行中删除返回 409
5. **杂项**：已归档条目不再重复显示"存原文"；队列历史同条目只保留最近一次结果；`WorkspaceManager.get()` 公开化供启动恢复使用
6. **开发迭代范式入档**（ADR 18，2026-09-29）：常驻容器热更循环（前端 Ctrl+F5 / 后端 `docker restart deepaggregator`）为日常唯一迭代方式，迭代期禁 compose/重建镜像、禁第二进程访问 workspace 库文件；§9 增补容器 SQLite `disk I/O error` 的标准恢复程序
7. **Reddit 存档与查看增强**（2026-09-29，ADR 12 修订）：存档 JSON 收敛为原帖 + top20 热门评论（`?limit=20&sort=hot`）；查看页内嵌媒体——v.redd.it 视频直接内嵌播放（fallback mp4 无独立音轨，页内注明并附原帖链接）、图集/直链图片/预览图直接显示，纯链接仅作无媒体时的回落
8. **YouTube 源与上传存档**（2026-09-29，ADR 19）：第四种源类型 youtube（频道 RSS 免 API key，@handle 自动解析 UC id 并缓存，解析优先 externalId > canonical > 裸匹配）；items 增 image/duration 列，feed 卡片为横向布局——4:3 封面图左置（右下角叠视频时长徽章）、标题+简介右排（压缩卡片高度）；详情面板对 YouTube 条目直接内嵌官方 iframe 播放器（不跳转即可观看，"信息流勿扰"）；时长经入库回填钩子 `enrich_items` 从 watch 页提取 lengthSeconds（每轮至多 10 条，找不到记 -1 永久跳过）；新增逐源 `allow_upload` 开关（默认关）与「上传存档」按钮——用户上传本地文件覆盖存档（覆盖语义同 ADR 8），上传完成经 SSE 广播全 UI 同步；viewer 支持 video/image 资产（上传的 mp4 直接页内播放）；上传接口为原始字节流 + `?filename=`（镜像无 python-multipart，刻意不走 multipart）；修复 viewer 回落链缺 `md` 分支导致 AI Markdown 存档无法查看的既有缺口
9. **UI 修复**（2026-09-29）：设置页勾选框与文字对齐（根因有二：`.settings-page label` 的 grid 规则覆盖 `.chk-line` 的 flex、`input{width:100%}` 把 checkbox 拉成整行宽）；Reddit 内嵌媒体新增 YouTube 链接帖分支——提取视频 id 内嵌官方 iframe 播放器，替代原先误落的静态缩略图
10. **源列表分组与拖拽排序**（2026-09-29）：侧栏按源类型分组（youtube/arxiv/reddit/rss），组头可点击（= 按类型筛选整组消息，`/api/items?type=`）、可折叠（localStorage 记忆）、显示组级统计（未读/总数）；组头为"类别标签"样式（去色点、大写弱化色），组内源缩进并带引导线以表达父子层级；组顺序与组内源顺序均支持 HTML5 拖拽调整并持久化（`sources.sort_order` + `ui_prefs` 表，`GET/PUT /api/source-prefs`）；feed 状态机增加 `type` 维度，与 source_id 互斥
11. **媒体载体与 POP**（2026-09-29）：页面上有一个常驻**隐形媒体载体**（顶栏"▶ 媒体"为其唯一可见痕迹）；POP 把视频挂载到载体后弹出浏览器原生窗口，**同一时刻只有一个播放视口**（弹出成功即暂停页内播放器）。两级弹出：reddit/上传视频等原生 `<video>` 走元素级画中画（requestPictureInPicture）；YouTube 因播放器是跨域 iframe（内部视频元素不可达）走**文档级画中画**（`documentPictureInPicture`，Chrome 116+，置顶系统窗口内嵌官方播放器）；两级都不可用时载体转为右下角**可见**迷你播放器兜底（绝不隐形出声）。载体常驻于文档，浏览/切换条目不影响播放。**画中画被关闭或被其它网站顶掉时，顶栏"▶ 媒体"按钮点亮（品牌色）并记录进度，点击即从原进度恢复弹出**——画中画全局唯一，此设计保证"看别的视频顶掉当前播放后可快速回来续看"（youtube 进度经文档窗口内的 `__ytPlayer` 每秒回传）
12. **Reddit 图文卡片与详情嵌入**（2026-09-29）：入库时提取预览图（preview 分辨率档 ≈360px 优先，直链图片次之）与视频时长（media.reddit_video.duration），feed 卡片与 YouTube 一致地"图左文右 + 时长徽章"；存量条目经 `enrich_items` 回填（逐条取 `/comments/<id>.json`，每轮 10 条；确认无图记 `image='-1'` 哨兵不再重试；注意该端点返回的是数组）；详情面板对 reddit 帖子同样内嵌媒体——播放地址优先取自已存档的帖子 JSON（同源，不依赖外网；reddit 反爬导致浏览器直连 .json 不可靠），视频帖未存档时给静态封面提示先存档

13. **Bilibili 源**（2026-09-29，ADR 19 扩展）：第五种源类型 bilibili（UP 主空间抓取：`/x/space/wbi/arc/search` + **wbi 签名**（nav 每日密钥 → 固定置换表混排 → md5）+ buvid3/buvid4 Cookie + 空间子页提取 `w_webid`）；**未登录请求会被 -352 风控拦截，故新增 `bilibili` 设置段（sessdata/bili_jct/dedeuserid）与设置页扫码登录**（passport 官方二维码接口 + 轮询，B站 App 扫码后凭据自动落库；也可手动粘贴 SESSDATA）——登录态请求稳定；列表接口自带封面与时长（feed 卡片开箱即得；hdslb CDN 有 Referer 防盗链，页面已设 `no-referrer` 策略）；详情内嵌官方 player.bilibili.com 播放器，POP 经载体走文档级画中画；视频本体同样走「上传存档」
14. **凭据问题提示**（2026-09-29）：源抓取失败时按类型判定"凭据类错误"（reddit：credentials/401/token；bilibili：-352/风控/登录）并登记到内存注册表（`services/source_health.py`）；顶栏 ❌ "凭据"按钮点亮，下拉列出受影响的类别+源名+错误摘要，**点击直达设置对应页**；`GET /api/credential-issues` 查询；保存凭据（设置更新/扫码登录成功）自动清除登记，抓取成功同样清除
15. **书签/稍后阅读**（2026-09-29）：items 增 `is_bookmarked` 列；卡片与详情面板各带 🔖 切换按钮（内联 SVG 图标：未标记灰描边、标记后蓝色填充，详情切换同步卡片状态）；工具栏新增 "🔖 书签" 筛选（`/api/items?bookmarked=true`），与未读/星标过滤正交组合；未读/书签/星标三者为**胶囊 toggle chip**（选中品牌色高亮 + 图标彩色，未选灰描边，触摸友好），书签 chip 图标与卡片 SVG 同源，顺序与 feed 卡片一致（未读→🔖→⭐）
16. **响应式布局整治**（2026-09-29）：抽屉断点从 900px 提至 **1100px**——900-1100 的"中间地带"原桌面布局会把 feed 挤压到几十像素且工具栏（z8, sticky 自成堆叠上下文）反盖详情面板；现 ≤1100 统一走 ☰ 抽屉 + 全宽 feed，>1100 侧栏 pin 常驻；详情面板显式 `z-index: 15/20`（窄屏全屏浮层）确保始终高于工具栏；窄屏隐藏分组折叠箭头并强制展开全部源（触摸目标过小）；窄屏工具栏来源下拉限宽消除横向溢出
17. **设置页拆分**（2026-09-29）：原"订阅源"页过长，拆为两页——"订阅源"（新增订阅表单）与"设置源"（选择已订阅源逐源配置：名称/地址/间隔/颜色/存档与上传开关）；顶栏精简：设置按钮仅图标、令牌移入设置首位"访问令牌"页
18. **来源筛选树形面板与语义默认**（2026-09-29）：搜索类型顺序调整为 语义/关键词/混合，**默认语义**（前端 state 与 /api/items 缺省值同步）；来源下拉改为**树形多选面板**——按类型分组的复选树（类型勾选联动全选/取消子源，子源可单独勾选，类型框三态），打开时回显当前过滤，确认才生效、取消还原；确认后折叠为 source_ids（全选类型折叠为 types），后端 `_filter_clause` 支持 `source_ids`/`types` CSV 多选（同现时按 **OR 并集** 过滤——部分选中源 + 整类型组合的语义）；触发按钮文案随选择动态变化（全部来源/类型名/已选 n 源）
19. **查看器 srcdoc 覆盖 src 修复**（2026-09-29）：打开非 JSON 原文时先设空 `srcdoc` 再设 `src`，规范上 srcdoc 属性存在即优先——arXiv PDF/网页快照在浮层中渲染为空白页的既有缺陷；改为 `removeAttribute("srcdoc")`，关闭时同样清理
20. **静态资源缓存版本参数**（2026-09-29）：`app.js`/`style.css` 引用加 `?v=` 版本参数——容器挂载开发模式下，浏览器缓存旧脚本会导致"改了不生效"的误判
21. **交互健壮性**（2026-09-29）：书签/星标详情按钮切换方向以按钮当前视觉状态取反（`it` 副本可能过期）；书签按钮用 `currentTarget` 并在 `await` 前捕获引用（SVG 子元素点击 + async 后 currentTarget 失效两个坑）
22. **AI 阅读助手**（2026-09-29 设计定稿并**完成实现**）——从"信息聚合"迈向"知识工作台"的核心模块
    - **定位**：页面底部常驻 AI 对话栈，把工具从"信息聚合"推进为"知识工作台"：阅读中遇陌生概念即时提问、跨条目对比、结合存档 PDF 深挖论文
    - **布局**：栈位于页面下缘，顶部拖柄拖曳调高度（20vh~80vh，存 localStorage），双击收起；三列 = 会话列表(220px 可折叠) / 对话区(flex) / context 列(260px 可关闭，窄屏默认关)；窄屏栈全屏化；顶栏新增 AI 入口按钮开关栈
    - **数据模型**（db.py 新表，CREATE IF NOT EXISTS 零迁移）：chat_sessions(id/title/created_at/updated_at)；chat_messages(id/session_id→CASCADE/role(user,assistant)/content/contexts_json/created_at + session 索引)；chat_session_items(session_id/item_id 复合主键)——**条目关联表只记强关联**（item/doc context），"列表： 50"属环境性上下文不建条目级关联（否则每会话污染 50 条目，相关模块变噪声）
    - **context 注入协议**：附件式而非文本前缀——输入框保持干净，context 以 chips 挂输入框上方（可逐个移除），发送时随消息自动注入。三类：`{type:'list'}` = 当前 feed 快照（前端组文本：每源一行标题+来源+日期，top 50）；`{type:'item', id}` = 选中条目（后端按 id 组装标题/来源/作者/摘要/链接）；`{type:'doc', id}` = 条目存档文档（后端读文件提取文本：MD/TXT 直读、HTML 用 html_to_text、**PDF 用 pymupdf 提取（ImportError 回落 pypdf）**，截断 20K 字符）；**PDF 视觉通道**：含图表的页（位图或密集矢量绘图簇判定）整页渲染 110dpi PNG（data URI，至多 6 页、`DA_PDF_FIGURE_PAGES` 可配）随用户消息走视觉通道——单次请求可能因此带数 MB 图像。**视频条目默认不支持注入**（只给元数据）。消息落库 contexts_json 保留原引用
    - **上下文预算**：列表块每条一行（50 条 ≈ 3-4K 字符）；单文档截 20K；历史保留最近 20 轮；不做静默自动摘要
    - **后端结构**：`core/services/ai_service.py`（ai_cfg/has_ai 复用 settings.ai 段；SYSTEM_PROMPT 阅读助手中文人设；stream_chat = OpenAI 兼容 /chat/completions stream 逐 delta yield；extract_doc_text 按 mime/后缀提取；build_context_blocks 组装 context 块并收集强关联 item_ids；build_ai_messages 拼系统提示+参考块+最近20轮+用户消息）；`web/chats.py` 路由（GET/POST /chats、GET /{id}/messages、DELETE /{id}、GET /{id}/context-items、GET /for-item/{item_id} 相关会话查询、POST /{id}/chat = SSE StreamingResponse：先落 user 消息+建 chat_session_items 关联+首条消息设标题，再逐 delta 转发，完成后落 assistant 消息）
    - **AI 配置**：完全复用 settings.ai 段（OpenAI 兼容 base_url/api_key/model，DeepSeek 预设）；未配 api_key 时对话区显示引导卡片跳设置 AI 服务页
    - **用户已确认**：pypdf 依赖接受；视频条目默认不注入（只元数据）；PDF 单文档截断 20K 字符
    - **实现状态：全部完成并实测**（容器已装 pypdf 6.19.0 并重启加载；前端 AI 栈/三列/流式/详情集成全部上线，真实 DeepSeek api_key 端到端验证通过；详细实现交接与踩坑见仓库根 AI_CHAT_PLAN.md）。**三类 context 注入全部实测**：列表（前端组 50 条缩略文本）、条目（后端按 id 组装）、文档（pypdf/html_to_text 提取，20K 截断）——AI 能准确概括注入列表的主题构成。对话区排版对齐 Open WebUI：用户消息右侧品牌气泡、AI 消息左侧浅底、marked + DOMPurify 渲染、**mermaid 图表**、代码块/表格样式；**图片上传**（🖼 按钮/粘贴，存 workspace/ai_media，随消息 base64 注入视觉模型，历史 media_json 持久化）；**停止生成**（AbortController 中断，部分输出保留）；实现交接与踩坑详见仓库根 AI_CHAT_PLAN.md
23. **AI 对话 UX 全面重构（Open WebUI 移植）**（2026-09-29）：用户判定自研对话 UX 不合格，要求通读 Open WebUI 源码并**全量照抄**其聊天交互/排版/嵌入显示。四个方向精读产出实现级规格（消息区/内容渲染管线/输入区/侧栏+占位），前端三件套整体重写（v=20260929i）
    - **消息区**：928px 居中列、用户消息灰色气泡右对齐（rounded-3xl、gray-50/850）、助手消息全宽（28px 圆角头像 + 模型名 + 15px/1.625 正文）；操作条 = 复制/编辑/朗读(TTS)/好评/差评/继续生成/重新生成/删除/时间戳，**最后一条常显、历史消息 hover-reveal**；流式 = 思考中 shimmer + 2px 灰光标（animate-pulse）+ 150ms 节流渲染，生成中不显示操作条；错误为浅底圆角框（info 图标）；滚动：距底 5px 判定 autoScroll，输入区上方居中浮动"滚到底部"按钮，流式 rAF 节流跟随
    - **消息操作（后端新增）**：`PATCH /chats/{s}/messages/{m}`（编辑，`truncate_after` 截断重发）、`DELETE .../messages/{m}`（用户消息成对删除其回复）、`PUT .../messages/{m}/rating`（±1 落库）、`PATCH /chats/{s}`（置顶/归档/重命名）、`POST /chats/{s}/clone`；chat 端点支持 `regenerate`（删尾重流）/`continuation`（向尾条 assistant 追加）模式，SSE 末尾回传 `message_id` 与 **AI 生成标题**（首轮后 complete() 生成 4-12 字标题，替代问题截断）；db 迁移 chat_sessions.pinned/archived、chat_messages.rating
    - **内容渲染管线**：marked→DOMPurify 后 DOM 后处理——代码块组件（圆角 16px 边框容器、语言标签头、**折叠/复制/预览**，正文恒 #0d1117 github-dark + Open WebUI 令牌配色覆盖）；**mermaid**：主题随系统（dark/default）、securityLevel loose + DOMPurify svg profile 净化、**拖拽平移 + Ctrl 滚轮缩放 + 下载 SVG/重置/复制源码**控件、失败红框回落保源码（注意：本仓库 vendored mermaid 的 render() 直接返回 svg 字符串而非 {svg}，需双形兼容）；**KaTeX**（$$/\[/\(/$ 五种定界符，点击复制 TeX）；GitHub 风格告警块（NOTE/TIP/IMPORTANT/WARNING/CAUTION 左边框色条）；表格 = 横向滚动 + 悬停复制(TSV)/导出 CSV(UTF-8 BOM)；行内代码点击复制；图片点击灯箱（下载/Esc 关）；**HTML/SVG 代码块「预览」→ 右侧工件面板**（sandbox iframe srcdoc + 复制/下载）
    - **输入区**：rounded-3xl 悬浮容器（border 三态、shadow-lg、blur 背景）、textarea 自增高（上限 384px）、Enter 发送/Shift+Enter 换行/Escape 停止/ArrowUp 编辑末条用户消息、粘贴与拖放图片（全栏 drop 遮罩"添加文件"）、**圆形发送按钮三态**（黑底白箭头/灰禁用/生成中变停止）、附件 chips（图片 40px 圆角缩略 + context 文件 chips 240px）置于输入框上方
    - **会话侧栏**：置顶分组（可折叠记忆）+ 日期分组（今天/昨天/过去 7 天/过去 30 天/N 月/年份）+ 已归档分组；条目 = hover 显「⋯」菜单（置顶/重命名(双击行内编辑)/归档/克隆/删除-确认框）+ 未悬停时显示 time-ago 后缀；顶部搜索框实时过滤；行样式对齐 ChatItem（32px 高、active 底 rgba(0,0,0,.035)/白.045）
    - **其余**：空态占位（问候 + 「建议」列表 45ms 瀑布入场、点击填充输入框）；顶栏聊天菜单（复制/导出 JSON/Markdown/TXT/置顶/归档/删除）；确认框与 toast（深色圆角，替换原生 confirm）；CSS 级 tooltip；新 vendor：katex 0.16.22（css+js+auto-render+fonts）、highlight.js 11.9.0（github-dark）
    - **修过的坑**：代码块按钮工厂在创建时解引用尚未初始化的 const（TDZ）导致单块渲染异常**中断整列消息渲染**（此前"历史消息丢失"即此因）——已修 + 单条/单块 try/catch 隔离 + 流式收尾 fallback 渲染；编辑→发送路径漏建 AbortController（null.signal 必抛）；发送后编辑框不关闭（补 aiReloadMessages）
    - **有意取舍**：Open WebUI 的兄弟分支树（‹n/m› 分支切换、Save As Copy、fork）依赖 childrenIds 树模型，本实现为线性模型——编辑重发=截断重生成，其余交互全量保留；评分后的 1-10 打分面板（依赖其反馈后端）简化为 ±1 + toast；TTS 用浏览器 speechSynthesis 实现"朗读"

24. **AI 对话微调**（2026-09-29，用户验收第 23 条后）：①助手消息头部显示**真实模型代号**（`GET /api/chats` 增 `model` 字段，取自 settings.ai，如 deepseek-flash），替代"AI 助手"；②**移除右侧常驻上下文列**——「＋列表」「＋文档」改为顶栏 pill 按钮（toggle，附加后品牌色高亮），输入框上方显示「＋当前列表/＋当前文档/＋当前条目」chip（点击移除）；context **一次性**：随下一条用户消息发送后即清空；已发送消息头部渲染「附加列表/附加文档/附加条目」提示 chip，**点击在右侧预览窗口查看 context 详情**（`POST /api/chats/context-preview` 复用 build_context_blocks 逐块提取，iframe srcdoc 渲染，跟随深浅色）；预览面板与 HTML/SVG 工件预览共用（通用化 aiArtifactShow，meta 带下载名/mime）。**同日二轮微调**：「＋文档」并入「＋条目」——条目已归档自动附加全部可注入文档、未归档仅附加摘要；chips 蓝色高亮（brand 边框+浅底），**点 X 才移除、点击本体即预览**（发送前可查看将附内容）；chips 与消息头提示改为具体文案——列表 `列表："关键词" | N sources | M条`（关键词/sources 数为元数据随 contexts_json 存档）、条目标题 24 字截断、文档用文件名；发送映射保留元数据仅剔除内部标记。**三轮：列表 context 收归 core**——原从 DOM 抓取卡片文本（会被浏览器自动翻译污染，且只有标题无摘要），改为 `GET /api/chats/list-context` 端点（复用 core.search_items 全套过滤/检索参数，DB 直查），格式 `# 标题
摘要`（每条摘要截 300 字），chip 元数据 q/sources 一并返回。**四轮：顶栏按钮为纯添加语义**（非 toggle）——点击即附加，完全重复才拒绝（列表同快照 flash 已附加、条目/文档按 id 去重），不同条目可无限叠加；移除只靠 chips 的 ×。**五轮：AI 栈脱离悬浮改为 flex 并列**——body 改 flex column（100vh、overflow hidden），主 UI 包进 #mainWrap（flex:1、自身滚动，sidebar/detail/工具栏 sticky 相对它），AI 栈为文档流内的 flex 兄弟，不再遮挡上方任何控件；高度经 CSS 变量 --ai-h 联动（拖柄/min/收起统一走 aiSetH，上限钳制保留上方 ≥240px），body.ai-open 时 detail 面板 max-height 随 --ai-h 收缩保证底部按钮始终可见；顺带修复裸 #carrierVideo/#carrierYtWrap（POP 载体）参与 flex 布局占位 150px 的问题（fixed 隐形化）；窄屏仍全屏浮层。**六轮：详情面板重排**——存档操作组（存原文/上传存档/删除存档/查看原文/归档状态）从底部 .actions 行移入头部（✕关闭 右侧同一水平，按钮压缩尺寸 + flex-wrap 兜底）；「✦ 加入 AI」按钮删除（与 AI 窗体顶栏「＋条目」重复）；文件名/尺寸（assetList）移至标题下方；「相关 AI 讨论」移至标题/文件名之下、来源 meta 与摘要之上。**七轮收敛**——头部仅留 关闭/存档(含重新存档/归档中)/删除存档/上传存档(仅 allow_upload)/书签/星标，「查看原文」按钮与「已归档」状态移除（文档名即入口：主文档点击走页内查看器 openViewerAsset，其余文档新窗口）；「相关 AI 讨论」更名「相关讨论」，条目按钮常驻细蓝边框、悬停整体变蓝、文本左对齐（样式在删上下文列时曾误删，已重建）；「打开原始链接」独立行取消，改为标题后空一格缀 🔗 emoji 超链接（新窗口）。**八轮：横向滚动清零**——detail 面板 overflow-x hidden + summary/meta/标题 overflow-wrap anywhere（长 URL 撑出水平滚动条的根因）；sidebar overflow-x hidden 且 AI 打开时 max-height 随 --ai-h 收缩（与 detail 同步，源列表全部可达、不再出现横向滚动）；main 卡片长链接兜底换行。**九轮：三栏顶边对齐**——工具栏加 margin-top 12px、detail 顶 padding 调至 11px，三栏首个可视元素（源组头/工具栏/关闭按钮）顶边统一在 12px 线（实测 top 均为 60px）。**十轮：查看窗口铺满信息区**——布局只有两大 flex 块【信息区 mainWrap】【AI 区】，#viewerOverlay 移入 mainWrap 内、absolute inset 0 覆盖信息区全部显示面积（z 45 盖过 toolbar/sidebar/detail），AI 区独立在下方不受影响，拖柄随时可拖；关闭查看窗口即还原主 UI（滚动位置无损）。此前一度改为第三段 flex 的方案已废弃。**附：拖柄拖拽 iframe 吞事件修复**——PDF 查看器是独立文档的 iframe，鼠标进入后 mousemove 不再到达父页，拖拽中断（"拖不上去、拽得下来"）；拖拽期间 body.ai-dragging 使全部 iframe pointer-events:none，松手恢复。**附：mermaid 视口自适应**——图表 stage 取消 420px 保守截断；左上角新增 放大/缩小/适应 三控件（前两者等价 Ctrl+滚轮；「适应」把视口高度调到正好容纳当前缩放下的整图，上限 85vh——矮图缩矮、长图扩展为可跨页滚动的完整视图，同时回中）；首次渲染完成即自动调用一次适应，流式结束无需操作即见视口适中的图表（修复：建议点击填充后同步 aiUpdateSendState，发送按钮恢复可用）。**附：图片上传损坏修复**——后端为原始字节流端点，前端却用 FormData（multipart）发送，边界/头一起被存进文件 → 图片全损坏（AI 靠容错仍大致识图）；前端改为 ArrayBuffer 直发；存量 6 个 multipart 残留文件已清除（历史消息中的图无法恢复，显示为裂图）。**公式输入优化（B 方案 + 复制）**——①发送兜底 aiAutoFormula：无 $ 定界符但含 LaTeX 命令+上下标的整段自动包 $$，杜绝被 markdown 吃掉；②输入框上方实时预览条（含公式/markdown 特征时浮现，150ms debounce，渲染=兜底后的实际发送效果）；③用户气泡含公式/markdown 时走同一 aiRenderMd 管线（与 AI 消息同渲染质量）；④全部 KaTeX 公式悬停右上角出现复制按钮，复制原始 LaTeX（KaTeX DOM 的 annotation 自带源码，无需额外存储；按钮收进公式框内——right 负偏移会撑出 katex-display 的横向滚动条）——复制→贴输入框改→预览确认→发送，研究型公式工作流闭环。**预览收敛为光标级**——原整段输入预览（长文时撑高输入区、遮挡历史）废弃；改为光标进入某个 LaTeX 公式内部时，只预览该公式（$$..$$/$..$/裸 LaTeX 整段三级定位），光标移出或输入框失焦即消失，Arrow 键移动光标同样联动；预览标题定为「公式预览」；公式复制带定界符——display 复制 $$…$$、行内复制 $…$，不熟悉语法的用户粘贴后预览/渲染行为一致。**渲染管线重构（修 $$ 跨行公式不渲染）**——markdown 先把公式里的 _ ^ {} 吃成斜体并切碎 DOM 节点，KaTeX auto-render 跨节点匹配不到；改为「占位符流水线」：代码段（``` 围栏/行内反引号）以外的文本先提取公式为占位符 → marked/DOMPurify 正常渲染 → TreeWalker 遍历文本节点还原为 katex.render（代码语境的占位符回写公式原文，不渲染）；KaTeX 侧复制按钮机制不变（annotation 仍带源码）。**PDF 上下文质量升级（视觉路线）**——PDF 文本提取 pypdf→pymupdf（公式字符映射显著更好，pypdf 保留回落）；新增 extract_doc_figures：含图表的页整页渲染 110dpi PNG（data URI，至多 6 页），**图表页判定 = 位图图像 或 密集矢量绘图簇**（学术论文的 matplotlib 图是矢量嵌入，仅位图探测会全部漏掉；启发式：位图存在且绘图≥30/面积>30k，或矢量路径≥60 且面积≥20k）；图表页以 OpenAI 兼容 image_url 随用户消息走视觉通道（system 文本注明页码），context 预览窗口同步渲染这些页图；regenerate/continuation 为纯文本（图不重发）；requirements 增 pymupdf。**思维链（CoT）支持**——推理模型（DeepSeek reasoner 系）的 delta.reasoning_content 单独转发为 SSE reasoning 事件，从首条思维链到首条正文计秒；chat_messages 增 reasoning_json（text+secs）落库，历史恢复后思维链保留；前端为可折叠块：思考中 = 旋转 spinner + shimmer「思考中…（N 秒）」实时计时（默认收起，点开可看思维链原文实时滚动），正文首 delta 到达即折叠并定格「已深度思考（用时 N 秒）」，点击展开查看全过程；中断/出错时已产生的思维链一并保存。**附：mermaid 升级 v12 + 新图型支持**——vendored mermaid 升至 12.0.0（保留 11.17.2 备份），支持 quadrantChart/xychart-beta/pie/sequence 等全部类型；三个兼容坑：①```quadrantChart 类 fence 的图类型在语言标记上，渲染前需补回规范驼峰名（mermaid 检测大小写敏感，marked 把语言转了小写）；②xychart-beta 词法器不支持 CJK（v11/v12 均如此）——渲染前把标题/轴标签里的 CJK 换成 ASCII 占位符（引号内整串优先、剩余裸词二次替换），SVG 文本节点再还原原文；③占位符必须纯 ASCII（私有区 Unicode 字符同样被词法器拒绝）。**附：AI 栈 position static → relative**——弹出菜单（aiPopMenu/aiChatMenu）以 AI 栈为定位祖先，static 化后吸附坐标全错，relative 恢复（flex 占位不受影响），随后打镜像 `zsydeepsky/deepaggregator:20260929-1549`（latest 同步）并推送 Docker Hub，离线包 docker_image/deepaggregator-20260929-1549.tar.gz（533.6 MB），随后打镜像 zsydeepsky/deepaggregator:20260930-0332（latest 同步）推送 Docker Hub，离线包 deepaggregator-20260930-0332.tar.gz（533.6 MB）——本版含翻译三档+逐段对照+任务生命周期、公式工作流、思维链、PDF 视觉上下文、mermaid v12、图片标注编辑器、长按充能批量已读、双击直达设置源、选中亮黄、存档面板改版全部成果
    - **已知风险**：Chromium 文档级画中画窗口打开期间的光标闪烁为 Chromium bug，与本模块无关但同屏使用时可能叠加感知；PDF 双栏/扫描版提取质量一般；上下文超限时按预算硬截断
25. **内置翻译**（2026-09-29，替代浏览器全页翻译，历经九轮迭代至 2026-09-30 定稿）
    - **主档（一轮）**：顶栏「🌐 翻译」面板（three-tier 选择行卡片 + 目标语言双列网格 + 底部【设置】跳设置页，视觉对齐「选择来源」面板）；Feed 卡片原地替换翻译（悬停 title 看原文），详情面板**逐段对照双语**——译文在上、原文变暗（opacity .55）在下；provider 可插拔（settings.translate 段：none/cloud/api + api_url/api_model/api_key/api_id/api_region，设置页「翻译」页可配 + 测试连接）；逐卡增量翻译，目标语为中文时 CJK 占比>20% 的文本跳过；transSame 统一防线——译文与原文规范化后完全一致时不上屏；翻译结果内存缓存（hash+lang）
    - **二轮（三档架构）**：本地 ONNX 推理移除（CPU 逐 token 太重），settings 增 translate 段（provider none/google/api），菜单三档切换（无翻译 / 云端 google.com / 翻译API 显示已配 URL + 目标语言 + 【设置】跳转），顶栏图标亮 = 已开启；api 档走任意 OpenAI 兼容端点（可自挂 Hy-MT2 等本地部署翻译模型）；model-cache/hy-mt2 目录删除（不入镜像）；transActive 以 provider!==none 判定
    - **三轮（多协议 + 反馈修正）**：①「翻译」排到向量模型之后；②api 档更名「翻译 API（自定义接入）」并新增多协议适配——OpenAI 兼容(LLM) / DeepL / 百度翻译(MD5) / 腾讯云 TMT(TC3-HMAC-SHA256) / 火山翻译(HMAC-SHA256 AK/SK) / 阿里云机器翻译(RPC HMAC-SHA1)，按类型显示字段，设「测试连接」试译样例；③cloud 档引擎可选：Google gtx 或 MyMemory（免 key 直连，450 字符分块、Autodetect 源语言）——必应 web 端点因 cn.bing 不再内联 token 放弃；旧 provider=google 档值前后端迁移为 cloud
    - **四轮（逐段对照）**：按原始摘要真实换行切段（DOM textContent 无 <br> 换行，不能从 DOM 切），每段「译文（亮）→ 原文（变暗）」交错，标题同样译文在上原标题变暗；修复刷新后翻译静默失效——transProvider 启动时 transLoadProvider() 加载并自动续跑
    - **五轮（卡片标题双语）**：Feed 卡片翻译后保留原始 title 在首行、译文 title 插其下（.item-title-trans），摘要仍整体替换——AI 引用原始标题时可定位卡片
    - **六轮（调度优化）**：①Feed 卡片进视口才翻译（IntersectionObserver，root=itemList，rootMargin 160px，失败卡 20s 冷却重试）；②优先级任务队列：详情摘要翻译 unshift 插队、视口卡片 push 排队；语言/通道变更清空队列
    - **七轮（快捷面板视觉对齐）**：翻译菜单从 .ai-menu 列表改为「选择来源」同款面板卡片（.trans-panel + 行卡片 tree-btn 选中品牌填充 + 语言双列网格 + 底部设置按钮）
    - **八轮（任务生命周期）**：队列任务携带 alive() 存活回调（Feed 卡=仍在 DOM 且未翻译；详情=面板仍停在该条目）；入队/出队/完成三时点失效检查，transPurge 遍历剔除死任务；每任务持 AbortController——等待中的死任务直接出队，在途请求一并中止（api() 透传 signal）
    - **九轮（逐段流式）**：详情翻译骨架先行——标题译文占位 + 逐段（译文占位呼吸闪烁 → 原文变暗），每段独立高优先级任务（倒序 unshift 保序），译完一段立刻回填一段
    - **transSame 防线**：译文与原文规范化（去空白、小写）后完全一致时不上屏——卡片不加译文标题/不替换摘要，详情撤占位并恢复原文亮度
    - **坑与修复**：①index.html 版本参数曾被改回旧值导致浏览器加载旧 app.js（功能"失效"），v 参数务必只进不退；②顶栏 .topbar 原 overflow:hidden 会裁剪内部 absolute 下拉菜单（按钮高亮但菜单不可见），已移除；③aiPopMenu 拉伸全宽——.ai-menu 默认 right:0 与 JS 设置的 left 并存导致两端锚定，设 right:auto；④选择来源面板从全屏 fixed 移入 mainWrap absolute（z46、高度随信息区收缩），AI 区拉起不再遮挡
    - **镜像**：zsydeepsky/deepaggregator:20260930-0332（latest 同步）已推送，离线包 deepaggregator-20260930-0332.tar.gz（561.2 MB）——含翻译全部成果、公式工作流、思维链、PDF 视觉、mermaid v12、图片标注编辑器、长按充能、双击直达设置源、选中亮黄、存档面板改版。**容器迁移至 compose 管理**：旧手动 docker run 容器（映射 57577）移除，compose 端口对齐 57577:8080 并从 latest 镜像重建；旧镜像 20260928-1832 已删
    - **补充记录（重建并回，均为已交付成果）**：①**AI 区 flex 并列**——body 改 flex column，主 UI 包进 #mainWrap（flex:1 自滚动），AI 栈为文档流兄弟（相对定位保留作菜单锚），高度经 --ai-h 联动（aiSetH 统一设置、上方 UI 保留 ≥240px），detail 面板随 AI 开合收缩；②**详情面板重排**——存档操作组入头部与「✕关闭」同排，标题下文档名即查看入口，相关讨论居标题/文件名下，原始链接改为标题后缀 🔗；③**顶栏 .topbar 移除 overflow:hidden**（下拉菜单不再被裁剪）；④**侧栏长按充能·批量已读**——组头/单源长按 1s 品牌色充能 → POST /api/items/mark-read 批量标读（core.mark_read_bulk），早释放取消、完成后 600ms 抑制 click；⑤**双击 source 卡片直达 设置→设置源** 并自动选中该源（loadAppSettings + refreshEditSources 后赋值触发 onchange）；⑥**选中卡片文本亮黄**（.detail-open 标题/摘要 #ffe14d，已读 opacity 恢复 1）；⑦**选中文本 → 🔍 定位 Feed 卡片**——AI 消息区选中文本浮出按钮，按标题双向包含匹配（含译文标题），命中滚动定位 + 1s 黄色闪烁（闪烁延迟到滚动落定后启动），未命中 toast

26. **开源前审计清理**（2026-09-30，外部 agent 审计 + 人工核实）
    - **P0-1 修复**：`delete_source()` 无条件删 `item_vectors`（vec0 惰性创建，新库缺表）→ 新库删源 500；改为 sqlite_master 探测存在才删（回归测试 tests/test_p0_regression.py）
    - **P0-2 修复**：`_scan_unlocked` 曾把 `assets/` 等保留目录当工作区（幻影"assets"工作区）、`_migrate_legacy` 因"存在任意合法子目录即 return"而永久死锁——现工作区判定 = 含 aggregator.db 或空目录，且排除 RESERVED_DIRS（assets/ai_media）；迁移条件改为 default 不存在才执行；已核实并清理遗留物（根 aggregator.db 空库、assets/aggregator.db 空库、assets/6e/** v1 哈希资产，default 库零引用）
    - **P0-3 修复**：.gitignore 的 `workspaces/` 目录名笔误 → `workspace/`（开源前防真实数据库入库）
    - **P1 修复**：/api/items 缺省 mode 对齐文档改 semantic（原 hybrid 与前端/文档相反）；ai_media 删未用 UploadFile 导入（multipart 残留）；AppSettings 的 section 列表收敛为 SECTIONS 常量（load/update 共用）；is_enqueued 补 _scheduled 检查
    - **P2**：DESIGN translate 段枚举对齐代码（none/cloud/api）；Hy-MT2 文案残留清理 4 处（compose 僵尸 env 2 行、translate.py 注释、设置页 option 与 placeholder）；PDF 提取链文档对齐（pymupdf 优先回落 pypdf + 视觉通道 ≤6 页 110dpi、DA_PDF_FIGURE_PAGES 可配）；DESIGN §7 API 表补全 /chats/*（13 端点）/translate/* /ai-media/* /items/mark-read 并去重 /stats
    - **P3**：新增 pyproject.toml（ruff：pyflakes+关键 pycodestyle，per-file-ignores 豁免 __init__ 重导出与 main 延后导入），全库 ruff 清零（顺带清 ai_service/embeddings 4 处死导入）；unique_asset_name 的 source_type 判定为审计误报（函数内实际参与 rel_path 拼接）；workspace 根的 docker_run.bat（用户脚手架）与 .current（运行时状态）留置不动
    - **版本**：v 参数已统一 20261004c；全部修复后 56 测试通过（含新增回归）
