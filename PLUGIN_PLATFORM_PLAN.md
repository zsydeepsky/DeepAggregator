# DeepAggregator 源插件平台重构计划（Plugin Platform Refactor）

> 状态：**目标定稿（2026-09-30），未开工**。基线快照 = git 标签 `v1.2-baseline`（根提交 7c2c51e）。
> 本文档是本次重构的**目标与验收依据**；各阶段落地后，既成事实由 DESIGN.md 滚动吸收，本文档随之标记阶段完成。

## 1. 愿景与问题陈述

**一句话**：把 DeepAggregator 从"内置五种源的聚合器"重构为**无内建源的插件平台——用户可以借 agent 为任意信息源编写爬虫插件**（小众网站、公司内部频道、任何可公开或凭据访问的信息流）。

- **品类第一痛点**是"我的源不支持"。人工维护 adapter 是所有聚合器（RSSHub/Folo 等）的永久负债；让 agent 按需**生成** adapter、并在源腐坏时**自愈** adapter，从根上解除这个瓶颈。
- **目标形态**：主干引擎本身不定义任何源；现有 rss/arxiv/reddit/youtube/bilibili 五种源全部迁出为外部插件。**五源迁出即 dogfooding，即本重构的验收标准**——契约不够用会在迁移中立刻暴露。
- **边界（红线）**：不是通用爬虫。不做配置驱动的 scrape DSL / YAML selector 方案（ADR 6 明确否决过"通用爬虫+全局配置"，v2 roadmap 的通用 scrape 适配器是另一物种）。做的是**把领域知识蒸馏成可复用代码组件（SDK），让 agent 写的是代码、过的是测试**。
- **信任模型**：完全私有化部署 + 用户自己的 agent。插件（无论进程内还是子进程）拥有容器级权限；**闸门是契约测试套件 + 用户 review，不是沙箱**。子进程隔离是故障边界而非安全边界——此句必须写入插件作者文档，防止误读为安全承诺。

## 2. 目标架构

```
仓库（trunk）
├── app/                        主干：core/web 边界不变；core 不再 import 任何具体源
│   ├── core/sources/
│   │   ├── base.py → 升格为 sources.sdk（契约导入面，版本化）
│   │   ├── loader.py           目录扫描 + importlib 动态加载 + reload API
│   │   └── worker.py           （阶段1）fetch worker 子进程 + 监督者
│   └── web/                    设置页 schema 驱动渲染插件设置段；统一 auth 流路由
├── sources/                    ★ 插件目录（容器挂载，宿主可写，agent 的工作区）
│   ├── README.md               agent 指南：契约 + SDK API 表 + 组件清单 + 坑清单
│   ├── _template/              插件骨架：source.py + config.json + fixtures/ + 测试样例
│   ├── rss/  arxiv/  youtube/  reddit/  bilibili/   ← 五个内建源迁出后的形态
│   └── <新源>/                  agent 按需生成的插件落点
│       ├── source.py           插件代码（只准 import sources.sdk + 白名单依赖）
│       └── config.json         manifest：元数据/契约版本/设置 schema/auth 声明/能力声明
└── tests/                      trunk 测试 + 插件契约测试套件（sourcekit 调用）

数据根（volume）
├── source_runtime/<type>/
│   ├── status.json             持久化失败分类学 + 最近 N 次错误 + 响应片段（自愈回路的传感器）
│   └── cache.json              凭据/会话磁盘缓存（wbi 密钥、OAuth token——子进程化后必须出进程）
```

- **加载**：启动时扫描 `sources/` 动态加载；`POST /api/admin/sources/reload` 热装载（agent 装完新源自续，无需整容器 restart）；manifest 校验失败、type 冲突、契约版本不匹配进"插件健康"面板，**不允许静默失败**。
- **执行模型（两阶段）**：阶段 0 进程内 + 全边界超时加固；阶段 1 fetch worker 子进程 + 监督者 + 隔离（见 D11）。

## 3. 契约决策清单（本次重构的 ADR）

| # | 决策 | 结论与理由 |
|---|------|-----------|
| D1 | **契约 = SDK 导入面** | 插件只准 `from sources.sdk import ...` + 标准库 + 白名单三方依赖。trunk 内部可自由重构，SDK 签名即兼容承诺（带独立版本号与弃用政策）；agent 只需读一个文件即知全部能力；import 白名单可被测试机械化执行 |
| D2 | **代码与运行时状态分离** | `sources/` 只放代码与 fixtures；失败状态/缓存写数据根 `source_runtime/<type>/`，防代码目录被运行时写穿（bind mount 教训） |
| D3 | **设置 UI schema 驱动** | 插件设置段以 JSON schema 声明（text/password/number/bool/select + secret + help + 测试连接），trunk 通用渲染。agent 交数据不交 UI 代码；translate 段多协议渲染即此模式先例 |
| D4 | **auth 流协议**：声明式优先、代码式兜底 | OAuth client_credentials 等纯 config.json 表达；交互式流程（扫码）由插件实现 `start_auth()/poll_auth()`，trunk 拥有统一路由 `POST /api/source-auth/{type}/start`、`GET .../poll`，凭据落该源设置段。bilibili 扫码登录为压舱石用例 |
| D5 | **SECTIONS 动态化** | appsettings 的"load/update 段一致"不变量改为 `基础段 + 插件声明段` 单一构建函数，杜绝"存得进读不回"旧坑复发 |
| D6 | **sources.type CHECK 放开** | 单事务表重建迁移去掉类型枚举约束，改为应用层注册表存在性校验；`get_source()` 对未知类型给可读错误（现 KeyError 会 500） |
| D7 | **viewer 词汇表封闭集** | view kind 是 trunk 拥有的封闭集（html/json/md/pdf/image/video/file/embed…），插件只能映射内容到既有 kind（config.json 可给 url_template 参数化模板）。**放任每源加前端分支 = trunk 稳定破产**。reddit 帖文树渲染器长期要么泛化为通用 JSON 渲染器入词汇表，要么标记为内建插件专属 |
| D8 | **失败分类学 + 持久化** | 失败分 `auth / parse / network / ratelimit / contract / crash` 六类（分类决定自愈策略：auth→引导换凭据；parse→agent 重写；ratelimit→退避；crash→隔离）；现状 source_health 为内存注册表重启即丢，升级为 `status.json` 持久化 |
| D9 | **测试套件分层 + sourcekit CLI** | offline 档（fixtures，秒级确定性，agent 主反馈回路）+ live 档（真打目标，可选容错）；`capture` 助手抓真实响应存 fixture（agent 无法凭空猜响应形状，此步决定成功率）；单一入口 `docker exec deepaggregator python -m sourcekit <type> check\|fetch\|capture` = agent 的 REPL |
| D10 | **依赖白名单** | 容器禁新 pip 依赖的既定政策入契约；测试第一关 = import 白名单扫描，防 agent 顺手 import 未装依赖搞挂容器 |
| D11 | **隔离两阶段** | 阶段 0：所有插件边界调用（fetch/archive_requests/enrich/viewer）统一 `asyncio.wait_for` 预算（fetch 120s、enrich 单条 30s 级），SDK 只提供异步网络助手（禁同步阻塞）。阶段 1：常驻 fetch worker 子进程 + 监督者；crash 感知 → 自动重启 → 同源连续崩溃 N 次 → **隔离**（自动 disable + 健康面板登记）。串行扫描循环下一个源挂死 = 全局抓取静默停摆，隔离是唯一根治 |
| D12 | **enrich_items 正式入契约** | YouTube 时长、Reddit 预览图回填直接决定卡片质量；契约与测试覆盖（含"确认无法获取记 -1 哨兵不再重试"语义） |
| D13 | **五源迁出 = dogfooding** | 迁移顺序 rss → arxiv → youtube → reddit → bilibili：前两个验证基本契约，bilibili 最后压测 D3+D4（schema 设置 + 扫码流）。迁移全程 56 测试绿、UX 零回归 |
| D14 | **契约版本号 + 健康可见性** | manifest 带 `contract_version`；不匹配明确报"需迁移"而非诡异崩溃；插件健康（校验错误/冲突/隔离状态）在 UI 可见 |
| D15 | **文档可执行化** | `sources/README.md` 按 agent 消费视角写（quickstart 走 _template → sourcekit check → reload）；**文档内示例代码必须被测试套件实际运行**——腐烂的文档对 agent 是毒药 |

## 4. 阶段计划

| 阶段 | 分支 | 内容 | 依赖 |
|------|------|------|------|
| M0 | `v1.2-baseline` (tag) | git 基线 + GitHub 远程（已完成） | — |
| M1 | `feature/source-contract` | loader（动态加载/reload API）+ manifest schema 校验 + D6 CHECK 迁移 + D5 SECTIONS 动态化 + 插件健康面板 | — |
| M2 | `feature/source-sdk` | base.py → `sources.sdk`（D1/D10/D12）+ sourcekit CLI + 测试套件分层 + capture（D9） | M1 |
| M3 | `feature/migrate-sources` | 五源迁出（D13 顺序），设置页 schema 渲染（D3）+ auth 流协议（D4）随 bilibili 落地 | M2 |
| M4 | `feature/source-runtime` | status.json 失败分类学 + 磁盘缓存助手（D8/D2）；viewer 词汇表封闭集收口（D7） | M3 |
| M5 | `feature/worker-isolation` | 阶段 0 边界加固 → worker 子进程 + 监督者 + 隔离（D11） | M4 |
| M6 | `feature/agent-guide` | `sources/README.md` + `_template/` + 文档示例入测试（D15） | M3（可与 M4/M5 并行） |
| — | （后续，不在本次范围） | 引擎暴露为 MCP server（方向 2b）、`run_source_tests/read_status/install_source` 工具面 | M6 |

每阶段合并回 `main` 的门槛：**全部测试通过 + 现有 UX 零回归**（前端改动 bump `?v=` 只进不退）。

## 5. 验收标准

1. **五源全部迁出**：`app/core/sources/` 不含任何具体源；五个插件在 `sources/` 下以纯外部插件形态运行，行为与迁移前一致（56 测试绿 + 抓取/归档/查看/设置端到端无回归）。
2. **毒源不伤主干**：任一插件抛异常/挂死/同步阻塞/CPU 自旋/子进程崩溃，应用本体与其他源的抓取不受影响；连续崩溃的源被自动隔离并在健康面板可见。
3. **热装载**：新插件放入 `sources/` → 通过 `sourcekit check` → reload API → 创建该类型订阅源 → 端到端出卡片，全程无需重建镜像或重启容器。
4. **终极验收（端到端 dogfood）**：一个**只读 `sources/README.md` 与 `_template/`、不接触 trunk 代码**的 agent，为一个小众站点（如 Hacker News / lobste.rs）新增源——通过契约测试、reload 后端到端跑通。此条通过，"agent 可接手的源拓展"即从愿景成为事实。
5. **自愈回路闭环**：人为破坏某插件模拟源腐坏 → status.json 出现正确分类 → 依文档指引由 agent 修复 → check 通过 → 恢复抓取。

## 6. 非目标（本次重构明确不做）

- 通用 scrape DSL / 声明式选择器配置（ADR 6 红线，v2 roadmap 单列）
- 插件市场 / 分发 / 签名机制
- 安全级沙箱（独立用户/namespace/seccomp）——本次只做**故障**隔离
- 前端组件化 / TypeScript 化
- MCP server（方向 2b，独立排期）
- 多用户 / 高可用（DESIGN v1 非目标继续有效）

## 7. 工作纪律（沿用既有约定）

- 常驻容器热更循环：前端改动 Ctrl+F5（`?v=` 只进不退）；后端改动 `docker restart deepaggregator`（等 15-20s）；迭代期禁 compose up / 重建镜像 / 新增 pip 依赖。
- **容器运行期间严禁第二个进程访问 `workspace/` 下的 .db 文件**（跨 OS SQLite 事故教训）。
- DESIGN.md 随各阶段落地滚动更新；本文档只标记阶段完成状态，不承担 as-built 职责。
