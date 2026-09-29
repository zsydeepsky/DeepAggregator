# AI 阅读助手 — 实现交接文档

> 更新：2026-09-29 | 状态：**全部完成**（后端已重启加载、前端上线、真实 api_key 端到端验证通过）
> 设计权威：DESIGN.md §13 第 22 条。本文件只记录实现状态、剩余步骤与踩坑，接续实现前必读。

## 已完成的代码改动（文件级清单）

1. **app/core/db.py** — 新增三表（CREATE IF NOT EXISTS，零迁移）：`chat_sessions`、`chat_messages`（session_id/role/content/contexts_json + session 索引）、`chat_session_items`（session_id/item_id 复合主键，条目↔会话强关联）
2. **requirements.txt** — 末尾追加 `pypdf`
3. **运行中的容器** — 已 `docker exec pip install pypdf`（6.19.0；`docker restart` 不丢，容器重建丢失需重装或等下轮镜像）
4. **app/core/services/ai_service.py** — 新文件，含：
   - `ai_cfg(app_settings)` / `has_ai(app_settings)`：复用 settings.ai 段（base_url 默认 deepseek、api_key、model）
   - `SYSTEM_PROMPT`：中文阅读助手人设（准确引用参考资料）
   - `stream_chat(messages, cfg)`：OpenAI 兼容 `/chat/completions` stream，逐 delta yield；非 200 抛 RuntimeError 含响应体前 200 字符
   - `extract_doc_text(path, mime)`：md/txt 直读、html→html_to_text、pdf→pypdf（ImportError→明确中文报错）；返回前 strip
   - `truncate(text, limit)`：超限加"原文共 N 字符"尾注
   - `build_context_blocks(contexts, db, assets_root, max_doc_chars)`：三类 context → 提示块；item/doc 收集 item_ids（强关联）；异常→错误块不中断；list 用 ctx["text"]（前端组好的缩略文本）
   - `build_ai_messages(history, context_text, user_content)`：SYSTEM_PROMPT + （参考块作第二个 system）+ 最近 20 轮 + 用户消息
5. **app/web/chats.py** — 新路由（prefix `/api/chats`，含 StreamingResponse 导入）：
   - `GET ""` 会话列表（含 message_count）、`POST ""` 新建（title≤60）、`DELETE /{id}`
   - `GET /{id}/messages`（contexts_json→contexts 解析）、`GET /{id}/context-items`
   - `GET /for-item/{item_id}`：相关会话查询（chat_session_items JOIN）
   - `POST /{id}/chat`（ChatIn: content≤32000 + contexts list[dict]）：落 user 消息 + chat_session_items 关联 + 首条消息截 40 字设标题 → SSE 流式（逐 delta；异常→error 事件）→ 完成落 assistant 消息 + 更新 updated_at → `[DONE]`
6. **app/main.py** — `include_router(chats_router)`
7. **app/core/services/search.py** — `_filter_clause`/`_recent`/`search` 支持 `source_ids`/`types` CSV 多选（IN 过滤）；**id_list 与 type_list 同现时必须 OR 并集**（面板"整类型+部分子源"组合语义）
8. **app/core/api.py / app/web/routes.py** — `/api/items` 透传 `source_ids`/`types`/`bookmarked`；mode 缺省改 **semantic**；`/api/credential-issues`、`/bilibili/login/qr(/poll)` 已上线

## 数据流要点

- **会话↔条目关联**：context type 为 `item`/`doc` 时收集 item_id → `chat_session_items`；type 为 `list` 不建关联（环境性上下文，避免每会话污染 50 条目）
- **上下文预算**：list 每条一行；doc 截 20K 字符；历史最近 20 轮（`build_ai_messages`）
- **前端 context chips**：发送时 contexts 数组随 body 传给 `/chat`，每轮重发当前 chips（UI 粘性）
- **相关 AI 讨论**：详情面板查 `/api/chats/for-item/{iid}`，点击 → 开 AI 栈 + 加载该会话

## 剩余实现步骤（前端为主，精确清单）

1. **`docker restart deepaggregator`** — 加载 chat 后端（chats 路由/新表已在文件）
2. **vendor marked.min.js** — `curl -L https://cdn.jsdelivr.net/npm/marked/marked.min.js -o app/web/static/vendor/marked.min.js`（MIT）；index.html 在 app.js 之前 `<script src="vendor/marked.min.js">`
3. **index.html** — ① topbar 加 AI 开关按钮（放媒体按钮左侧，如 `✦ AI`）② body 级 AI 栈 DOM：`#aiStack`（fixed bottom，含拖柄 `#aiStackHandle` + 头部[会话标题/关闭] + 三列 `#aiSessions`/`#aiChatCol`(消息区+context chips+输入行)/`#aiCtxCol`）
4. **style.css** — `.ai-stack { position: fixed; bottom 0; left 0; right 0; height: var(--ai-h, 40vh); z-index 50; }`、拖柄（cursor: ns-resize, 高 10px）、三列 grid（220px/1fr/260px，窄屏单列）、消息气泡、context chips、窄屏（≤900px）栈全屏化；已完成：消息排版（用户右/助手左气泡）、流式 markdown 渐进渲染（节流 90ms + 完成终渲）、mermaid 图表渲染、图片上传（/api/ai-media + 粘贴 + 预览移除 + 随消息发送）、DOMPurify 消毒、停止按钮（AbortController）
5. **app.js** — ① 栈开关（topbar 按钮）+ 拖柄拖拽（mousedown/mousemove 调 `--ai-h`，范围 200px~80vh，存 localStorage `da_ai_h`）② 会话列表渲染/切换/新建/删除 ③ 消息渲染（marked.parse + 代码块样式）④ SSE 流式：fetch POST `/api/chats/{id}/chat`（body JSON {content, contexts}）→ reader 逐行解析 `data:` → delta 追加到最后 assistant 气泡 ⑤ context chips：三个来源（当前 feed 快照 / 详情打开的条目 / 条目存档文档），chips 可单独移除，随消息发送 ⑥ 详情面板集成：actions 区"加入 AI 对话"按钮（addItemCtx）+ 底部"相关 AI 讨论"模块（`/api/chats/for-item/{iid}`，点击开栈加载会话）
6. **收尾**：窄屏（≤900px）栈全屏化；发送后自动滚底；AI 未配置时引导卡片

## 已知交互点 / 踩坑（勿重蹈）

1. `e.currentTarget` 在 `await` 后为 null——async 事件处理器先捕获引用（书签按钮曾中招）
2. iframe `srcdoc` 属性存在时优先于 `src`——设空 srcdoc = 白屏（PDF 查看器 bug 根因，已修复）
3. **容器运行时严禁 Windows 侧 sqlite3 直碰 `workspace/*.db`**——跨 OS 锁导致容器 500（unable to open database file）；恢复程序 = 停容器 → integrity_check + wal_checkpoint(TRUNCATE) → 起容器（今日已两次触发并恢复）
4. 静态资源缓存：index.html 已用 `?v=20260929a`；**改 app.js/style.css 后需递增版本号**，否则浏览器缓存旧脚本
5. 面板/浮层的 CSS 必须与 DOM/JS 同步交付（来源面板曾因漏 CSS 渲染成裸复选框条）
6. B站 api 请求必须带浏览器 UA（_headers 已封装）；-352 风控需登录态（settings bilibili 段 SESSDATA，扫码登录已实现）

## 验证清单

- [x] `docker restart deepaggregator` 后 `GET /api/chats` 200、`POST /api/chats` 201
- [x] 配置真实 api_key 后 `POST /api/chats/{id}/chat` SSE 流式输出、消息落库（chat_messages）
- [x] context chips：添加/移除/随消息发送；`contexts_json` 落库
- [x] 详情面板：加入 AI 对话 → chips 出现；相关 AI 讨论模块列出关联会话；点击跳转会话
- [x] 窄屏（≤900px）栈全屏化可用
- [x] 图片上传（🖼 按钮/粘贴）、预览、随消息发送（/api/ai-media，20MB 上限，uuid 落盘）
- [x] 流式 markdown 渐进渲染（150ms 节流 + 光标）+ mermaid 图表 + DOMPurify 消毒
- [x] 停止按钮（AbortController；中断后部分输出已落库）
- [x] 相关 AI 讨论模块 + 加入 AI 对话按钮（详情面板）
- [x] 列表 context 注入实测：AI 准确概括 50 条缩略列表的主题构成
- [x] 上下文列关闭/恢复、"＋ 当前列表 / ＋ 该条目文档"按钮均可用
- [ ] DESIGN.md §13 item 22 标注"已完成"
