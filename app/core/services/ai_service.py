"""AI 阅读助手服务：OpenAI 兼容流式对话 + context 组装 + 存档文档文本提取。"""

from __future__ import annotations

import base64

import json

import httpx

MAX_DOC_CHARS = 20000
MAX_HISTORY_MESSAGES = 20

SYSTEM_PROMPT = (
    "你是 DeepAggregator 的阅读助手，帮助用户理解、对比和深挖信息聚合器中"
    "的文章、论文与视频条目。回答默认使用中文（除非用户使用其他语言），"
    "准确引用所提供的参考资料；引用条目时注明其标题。"
)


def ai_cfg(app_settings) -> dict:
    d = app_settings.data.get("ai") or {}
    return {
        "base_url": (d.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
        "api_key": d.get("api_key") or "",
        "model": d.get("model") or "deepseek-chat",
    }


def has_ai(app_settings) -> bool:
    c = ai_cfg(app_settings)
    return bool(c["api_key"])


def extract_doc_text(path, mime: str = "") -> str:
    """从已存档文档提取纯文本（md/txt 直读、html 剥标签、pdf 用 pymupdf，缺失时回落 pypdf）。"""
    suffix = path.suffix.lower()
    text = ""
    if suffix in (".md", ".txt") or mime.startswith("text/"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suffix in (".html", ".htm"):
        from app.core.services.fulltext import html_to_text

        text = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    elif suffix == ".pdf" or mime == "application/pdf":
        try:
            import pymupdf

            with pymupdf.open(str(path)) as doc:
                text = "\n".join(page.get_text("text") for page in doc)
        except ImportError:
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            except ImportError as exc:
                raise RuntimeError("服务端缺少 pymupdf/pypdf，无法提取 PDF 文本") from exc
        except Exception as exc:
            raise RuntimeError(f"PDF 文本提取失败：{exc}") from exc
    else:
        raise RuntimeError(f"不支持的文档类型：{suffix or '(无后缀)'}")
    return text.strip()


MAX_FIGURE_PAGES = int(__import__("os").getenv("DA_PDF_FIGURE_PAGES", "6"))


def extract_doc_figures(path, mime: str = "", max_pages: int = MAX_FIGURE_PAGES) -> list:
    """提取 PDF 中含图表的页：整页渲染为 PNG data URI（供视觉模型直接看图）。

    返回 [{"page": 页码, "data": "data:image/png;base64,..."}, ...]；非 PDF 或无 pymupdf 时为空。
    """
    suffix = path.suffix.lower()
    if not (suffix == ".pdf" or mime == "application/pdf"):
        return []
    try:
        import pymupdf
    except ImportError:
        return []
    out = []
    try:
        with pymupdf.open(str(path)) as doc:
            for i, page in enumerate(doc):
                if len(out) >= max_pages:
                    break
                try:
                    # 图表页判定：位图图像，或密集矢量绘图簇（matplotlib 类矢量图）
                    # 学术论文的图多为矢量嵌入，仅靠位图探测会全部漏掉
                    infos = page.get_image_info()
                    dr = page.get_drawings()
                    draw_area = sum(d["rect"].get_area() for d in dr)
                    is_fig = (len(infos) >= 1 and (len(dr) >= 30 or draw_area > 30000)) or (
                        len(dr) >= 60 and draw_area >= 20000
                    )
                    if not is_fig:
                        continue
                    pix = page.get_pixmap(dpi=110)
                    b64 = base64.b64encode(pix.tobytes("png")).decode()
                    out.append({"page": i + 1, "data": "data:image/png;base64," + b64})
                except Exception:
                    continue
    except Exception:
        return []
    return out
def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文共 {len(text)} 字符）"


def build_context_blocks(
    contexts: list[dict], db, assets_root, max_doc_chars: int = MAX_DOC_CHARS
) -> tuple:
    """把前端传来的 context 引用数组组装为提示文本块。

    返回 (拼接文本, 关联条目 id 列表, 图表页图像列表)。
    list 为环境性上下文不建条目关联；item/doc 为强关联，写入 chat_session_items。
    图表页：PDF 含嵌入图像的页整页渲染为 PNG（data URI），随用户消息走视觉通道。
    """

    blocks: list[str] = []
    item_ids: list[int] = []
    doc_figures: list[dict] = []
    for ctx in contexts or []:
        ctype = ctx.get("type")
        try:
            if ctype == "item":
                iid = int(ctx["id"])
                it = db.one(
                    "SELECT i.*, s.name AS src FROM items i"
                    " JOIN sources s ON s.id = i.source_id WHERE i.id = ?",
                    (iid,),
                )
                if it:
                    blocks.append(
                        f"【条目：{it['title']}】\n来源：{it['src']} · 作者：{it['author'] or '佚名'}"
                        f" · 链接：{it['url']}\n摘要：{it['summary'] or '（无摘要）'}"
                    )
                    item_ids.append(iid)
            elif ctype == "doc":
                aid = int(ctx["id"])
                a = db.one(
                    "SELECT a.*, i.title AS item_title, i.id AS item_id FROM assets a"
                    " JOIN items i ON i.id = a.item_id WHERE a.id = ?",
                    (aid,),
                )
                if not a:
                    continue

                text = ""
                try:
                    text = extract_doc_text(assets_root / a["path"], a["mime"])
                except RuntimeError as e:
                    blocks.append(f"【文档 {a['name']}：{e}】")
                    continue
                note = ""
                try:
                    figs = extract_doc_figures(assets_root / a["path"], a["mime"])
                    if figs:
                        doc_figures.extend(figs)
                        pages = ",".join(str(f["page"]) for f in figs)
                        note = f"\n（本文档含图表的第 {pages} 页已作为图像附于本条消息）"
                except Exception:
                    pass
                blocks.append(
                    f"【文档：{a['name']}（条目：{a['item_title']}）】\n"
                    + truncate(text, max_doc_chars)
                    + note
                )
                item_ids.append(a["item_id"])
            elif ctype == "list" and ctx.get("text"):
                blocks.append(f"【当前列表（{ctx.get('count', '')} 条缩略）】\n{ctx['text']}")
        except Exception as e:
            blocks.append(f"【context 加载失败：{e}】")
    return ("\n\n".join(blocks), item_ids, doc_figures)


async def stream_chat(messages: list[dict], cfg: dict):
    """OpenAI 兼容流式对话，逐段 yield 文本增量。"""
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={"model": cfg["model"], "messages": messages, "stream": True},
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"AI 服务错误 {resp.status_code}: {body[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                    d = (chunk.get("choices") or [{}])[0].get("delta", {})
                    reasoning = d.get("reasoning_content") or d.get("reasoning")
                    if reasoning:
                        yield ("reasoning", reasoning)
                    delta = d.get("content")
                    if delta:
                        yield ("delta", delta)
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue


async def complete(messages: list[dict], cfg: dict) -> str:
    """非流式补全：内部复用 stream_chat，聚合为完整文本。"""
    parts: list[str] = []
    async for kind, delta in stream_chat(messages, cfg):
        if kind == "delta":
            parts.append(delta)
    return "".join(parts)


def build_title_messages(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "为下面的对话生成一个 4-12 字的简短中文标题。直接输出标题本身，"
            "不要引号、句号、前缀或任何其他内容。",
        },
        {
            "role": "user",
            "content": f"用户：{user_text[:500]}\n\n助手：{assistant_text[:500]}",
        },
    ]


def build_ai_messages(
    history: list[dict],
    context_text: str,
    user_content: str | None = None,
    context_images: list[dict] | None = None,
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context_text:
        messages.append(
            {"role": "system", "content": "以下是用户附上的参考资料：\n\n" + context_text}
        )
    for h in history[-MAX_HISTORY_MESSAGES:]:
        messages.append({"role": h["role"], "content": h["content"]})
    if user_content:
        # 视觉通道：文档图表页以 image_url 随本条用户消息发送（OpenAI 兼容格式）
        if context_images:
            parts = [{"type": "text", "text": user_content}]
            for fig in context_images:
                parts.append({"type": "image_url", "image_url": {"url": fig["data"]}})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": user_content})
    return messages
