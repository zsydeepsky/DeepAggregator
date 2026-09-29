"""AI 阅读助手路由：会话 CRUD + 流式对话 + 条目关联查询。"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.services import ai_service

async def require_auth(request: Request) -> None:
    from app.config import settings as app_cfg

    if app_cfg.auth_token:
        supplied = request.headers.get("authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        query_token = request.query_params.get("token", "")
        if supplied != app_cfg.auth_token and query_token != app_cfg.auth_token:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="unauthorized")


router = APIRouter(prefix="/api/chats", dependencies=[Depends(require_auth)])


def _db(request: Request):
    return request.app.state.core.db


def _core(request: Request):
    return request.app.state.core


class ChatIn(BaseModel):
    content: str = Field(default="", max_length=32000)
    contexts: list[dict] = Field(default_factory=list)
    media: list[dict] = Field(default_factory=list)  # [{url, name}] 图片附件
    regenerate: bool = False  # 重新生成：丢弃末尾 assistant 回复后重流
    continuation: bool = False  # 继续生成：向末尾 assistant 回复追加内容


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    pinned: bool | None = None
    archived: bool | None = None


class MessagePatch(BaseModel):
    content: str = Field(min_length=1, max_length=32000)
    truncate_after: bool = False  # 同时删除该消息之后的所有消息


class RatingIn(BaseModel):
    rating: int  # 1 / -1 / 0（取消）


class ContextPreviewIn(BaseModel):
    contexts: list[dict] = Field(default_factory=list)


@router.get("")
def list_sessions(request: Request):
    db = _db(request)
    rows = db.all(
        "SELECT s.id, s.title, s.pinned, s.archived, s.created_at, s.updated_at,"
        " COUNT(m.id) AS message_count"
        " FROM chat_sessions s LEFT JOIN chat_messages m ON m.session_id = s.id"
        " GROUP BY s.id ORDER BY s.pinned DESC, s.updated_at DESC, s.id DESC"
    )
    model = ""
    if ai_service.has_ai(_core(request).app_settings):
        model = ai_service.ai_cfg(_core(request).app_settings)["model"]
    return {"sessions": [dict(r) for r in rows], "model": model}


@router.post("", status_code=201)
def create_session(request: Request, body: dict | None = None):
    db = _db(request)
    title = ((body or {}).get("title") or "新对话").strip()[:60] or "新对话"
    cur = db.conn.execute("INSERT INTO chat_sessions(title) VALUES (?)", (title,))
    db.conn.commit()
    return {"id": cur.lastrowid, "title": title}


@router.patch("/{session_id}")
def update_session(request: Request, session_id: int, body: SessionPatch):
    db = _db(request)
    sets, params = [], []
    if body.title is not None:
        sets.append("title = ?")
        params.append(body.title.strip()[:120] or "新对话")
    if body.pinned is not None:
        sets.append("pinned = ?")
        params.append(1 if body.pinned else 0)
    if body.archived is not None:
        sets.append("archived = ?")
        params.append(1 if body.archived else 0)
    if not sets:
        raise HTTPException(status_code=422, detail="无可更新字段")
    params.append(session_id)
    db.conn.execute(
        f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?", params
    )
    db.conn.commit()
    return {"ok": True}


@router.post("/{session_id}/clone", status_code=201)
def clone_session(request: Request, session_id: int):
    db = _db(request)
    sess = db.one("SELECT title FROM chat_sessions WHERE id = ?", (session_id,))
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    cur = db.conn.execute(
        "INSERT INTO chat_sessions(title) VALUES (?)",
        ((sess["title"][:54] + "（副本）")[:60],),
    )
    new_id = cur.lastrowid
    db.conn.execute(
        "INSERT INTO chat_messages(session_id, role, content, contexts_json, media_json)"
        " SELECT ?, role, content, contexts_json, media_json FROM chat_messages"
        " WHERE session_id = ? ORDER BY id",
        (new_id, session_id),
    )
    db.conn.execute(
        "INSERT OR IGNORE INTO chat_session_items(session_id, item_id)"
        " SELECT ?, item_id FROM chat_session_items WHERE session_id = ?",
        (new_id, session_id),
    )
    db.conn.commit()
    return {"id": new_id, "title": (sess["title"][:54] + "（副本）")[:60]}


@router.delete("/{session_id}", status_code=204)
def delete_session(request: Request, session_id: int):
    db = _db(request)
    db.conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    db.conn.commit()


@router.get("/{session_id}/messages")
def list_messages(request: Request, session_id: int):
    db = _db(request)
    sess = db.one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    msgs = db.all(
        "SELECT id, role, content, contexts_json, media_json, rating, reasoning_json, created_at"
        " FROM chat_messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    out = []
    for m in msgs:
        d = dict(m)
        if d.get("reasoning_json"):
            try:
                r = json.loads(d.pop("reasoning_json") or "null")
                d["reasoning"] = r.get("text") or ""
                d["reasoning_secs"] = r.get("secs") or 0
            except ValueError:
                d["reasoning"] = ""
        d.pop("reasoning_json", None)
        try:
            d["contexts"] = json.loads(d.pop("contexts_json") or "[]")
        except ValueError:
            d["contexts"] = []
        try:
            d["media"] = json.loads(d.pop("media_json") or "[]")
        except ValueError:
            d["media"] = []
        out.append(d)
    return {"session": dict(sess), "messages": out}


@router.patch("/{session_id}/messages/{message_id}")
def update_message(request: Request, session_id: int, message_id: int, body: MessagePatch):
    db = _db(request)
    row = db.one(
        "SELECT id FROM chat_messages WHERE id = ? AND session_id = ?",
        (message_id, session_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    content = body.content.strip()
    db.conn.execute(
        "UPDATE chat_messages SET content = ? WHERE id = ?", (content, message_id)
    )
    if body.truncate_after:
        db.conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ? AND id > ?",
            (session_id, message_id),
        )
    db.conn.execute(
        "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.conn.commit()
    return {"ok": True}


@router.delete("/{session_id}/messages/{message_id}", status_code=204)
def delete_message(request: Request, session_id: int, message_id: int, after: bool = False):
    db = _db(request)
    if after:
        db.conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ? AND id >= ?",
            (session_id, message_id),
        )
    else:
        db.conn.execute(
            "DELETE FROM chat_messages WHERE id = ? AND session_id = ?",
            (message_id, session_id),
        )
    db.conn.execute(
        "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.conn.commit()


@router.put("/{session_id}/messages/{message_id}/rating")
def rate_message(request: Request, session_id: int, message_id: int, body: RatingIn):
    db = _db(request)
    val = body.rating if body.rating in (1, -1) else None
    db.conn.execute(
        "UPDATE chat_messages SET rating = ? WHERE id = ? AND session_id = ?",
        (val, message_id, session_id),
    )
    db.conn.commit()
    return {"ok": True, "rating": val}


@router.get("/{session_id}/context-items")
def session_context_items(request: Request, session_id: int):
    """与该会话关联（作为 context 注入过）的条目 id 列表。"""
    rows = _db(request).all(
        "SELECT item_id FROM chat_session_items WHERE session_id = ?", (session_id,)
    )
    return {"item_ids": [r["item_id"] for r in rows]}


@router.get("/for-item/{item_id}")
def chats_for_item(request: Request, item_id: int):
    """与指定条目相关的会话（该条目作为 context 注入过）。"""
    rows = _db(request).all(
        "SELECT s.id, s.title, s.updated_at,"
        " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count"
        " FROM chat_session_items ci JOIN chat_sessions s ON s.id = ci.session_id"
        " WHERE ci.item_id = ? ORDER BY s.updated_at DESC",
        (item_id,),
    )
    return {"sessions": [dict(r) for r in rows]}


@router.get("/list-context")
async def list_context(
    request: Request,
    q: str = "",
    mode: str = "semantic",
    source_ids: str = "",
    types: str = "",
    unread: bool = False,
    starred: bool = False,
    bookmarked: bool = False,
    limit: int = 50,
):
    """由 core 直接查库组装当前列表上下文（不经 DOM，免疫浏览器翻译）。"""
    core = _core(request)
    if mode not in {"hybrid", "keyword", "semantic"}:
        mode = "semantic"
    limit = max(1, min(limit, 100))
    d = await core.search_items(
        q, mode, None, unread, starred, limit, 0, "", bookmarked, source_ids, types
    )
    lines: list[str] = []
    src_names: set[str] = set()
    for it in d["items"]:
        src_names.add(it.get("source_name") or "")
        title = (it.get("title") or "(无标题)").strip()
        summary = (it.get("summary") or "").strip()
        if len(summary) > 300:
            summary = summary[:300] + "…"
        lines.append(f"# {title}\n{summary}" if summary else f"# {title}")
    return {
        "count": len(lines),
        "sources": len(src_names - {""}),
        "q": (q or "").strip() or None,
        "text": "\n\n".join(lines),
    }


@router.post("/context-preview")
def context_preview(request: Request, body: ContextPreviewIn):
    """逐块渲染 context 的可读文本，供前端在预览窗口展示。"""
    core = _core(request)
    db = _db(request)
    assets_root = core.manager.current().path / "assets"
    out = []
    for ctx in body.contexts[:20]:
        ctype = ctx.get("type")
        try:
            text, _, figs = ai_service.build_context_blocks([ctx], db, assets_root)
            out.append({
                "type": ctype,
                "text": text[:12000],
                "figures": [{"page": f["page"], "data": f["data"]} for f in figs],
            })
        except Exception as e:
            out.append({"type": ctype, "text": f"【context 加载失败：{e}】", "figures": []})
    return {"blocks": out}


@router.post("/{session_id}/chat")
async def chat(request: Request, session_id: int, body: ChatIn):
    core = _core(request)
    db = _db(request)
    app_settings = core.app_settings
    if not ai_service.has_ai(app_settings):
        raise HTTPException(
            status_code=400,
            detail="未配置 AI 服务：请到 设置 → AI 服务 填写 API Key",
        )
    sess = db.one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")

    special = body.regenerate or body.continuation
    if body.regenerate:
        # 重新生成：丢弃末尾的 assistant 回复
        db.conn.execute(
            "DELETE FROM chat_messages WHERE id = ("
            "  SELECT id FROM chat_messages"
            "  WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1)",
            (session_id,),
        )
        db.conn.commit()
        content = ""
    elif body.continuation:
        content = ""
    else:
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(status_code=422, detail="消息内容为空")

    if content:
        db.conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, contexts_json, media_json)"
            " VALUES (?, 'user', ?, ?, ?)",
            (
                session_id,
                content,
                json.dumps(body.contexts, ensure_ascii=False),
                json.dumps(body.media, ensure_ascii=False),
            ),
        )

    assets_root = core.manager.current().path / "assets"
    history = [
        dict(h)
        for h in db.all(
            "SELECT id, role, content, contexts_json FROM chat_messages"
            " WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    ]
    if special:
        # 重新生成 / 继续生成：沿用最近一条 user 消息附带的上下文
        ctx = next(
            (
                json.loads(m["contexts_json"] or "[]")
                for m in reversed(history)
                if m["role"] == "user"
            ),
            [],
        )
    else:
        ctx = body.contexts
    context_text, item_ids, context_figures = ai_service.build_context_blocks(
        ctx, db, assets_root
    )
    if content and item_ids:
        for iid in item_ids:
            db.conn.execute(
                "INSERT OR IGNORE INTO chat_session_items(session_id, item_id)"
                " VALUES (?, ?)",
                (session_id, iid),
            )
    if content and sess["title"] == "新对话":
        # 先用问题占位；AI 生成标题后在流里回传正式标题
        db.conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?",
            (content[:40], session_id),
        )
    db.conn.execute(
        "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.conn.commit()

    user_content = None if special else content
    # 图表页仅随本次发送走视觉通道（regenerate/continuation 为纯文本续写）
    messages = ai_service.build_ai_messages(
        history, context_text, user_content,
        context_images=context_figures if not special else None,
    )
    if body.continuation:
        messages.append(
            {"role": "user", "content": "继续。从中断处接着输出，不要重复已有内容。"}
        )
    cfg = ai_service.ai_cfg(app_settings)

    async def gen():
        full: list[str] = []
        reasoning: list[str] = []
        r_start = None
        r_secs: int | None = None
        try:
            async for kind, chunk in ai_service.stream_chat(messages, cfg):
                if kind == "reasoning":
                    if r_start is None:
                        r_start = time.monotonic()
                    reasoning.append(chunk)
                    yield "data: " + json.dumps({"reasoning": chunk}, ensure_ascii=False) + "\n\n"
                else:
                    if r_start is not None and r_secs is None:
                        r_secs = round(time.monotonic() - r_start)
                        yield "data: " + json.dumps({"reasoning_secs": r_secs}) + "\n\n"
                    full.append(chunk)
                    yield "data: " + json.dumps({"delta": chunk}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}, ensure_ascii=False) + "\n\n"
        text = "".join(full)
        reasoning_text = "".join(reasoning)
        reasoning_json = (
            json.dumps({"text": reasoning_text, "secs": r_secs or 0}, ensure_ascii=False)
            if reasoning_text
            else None
        )
        mid = None
        if text or reasoning_text:
            if body.continuation:
                last = db.one(
                    "SELECT id, content FROM chat_messages"
                    " WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
                if last:
                    db.conn.execute(
                        "UPDATE chat_messages SET content = ? WHERE id = ?",
                        (last["content"] + text, last["id"]),
                    )
                    if reasoning_json:
                        db.conn.execute(
                            "UPDATE chat_messages SET reasoning_json = ? WHERE id = ?",
                            (reasoning_json, last["id"]),
                        )
                    mid = last["id"]
            if mid is None:
                cur = db.conn.execute(
                    "INSERT INTO chat_messages(session_id, role, content, reasoning_json)"
                    " VALUES (?, 'assistant', ?, ?)",
                    (session_id, text, reasoning_json),
                )
                mid = cur.lastrowid
            elif reasoning_json:
                db.conn.execute(
                    "UPDATE chat_messages SET reasoning_json = ? WHERE id = ?",
                    (reasoning_json, mid),
                )
            db.conn.execute(
                "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )
            db.conn.commit()
        if mid is not None:
            yield "data: " + json.dumps({"message_id": mid}) + "\n\n"
        # 首轮对话后用 AI 生成正式标题（Open WebUI 的 Title Auto-Generation）
        if text:
            first_user = next((m["content"] for m in history if m["role"] == "user"), content)
            n_assistant = len(db.all(
                "SELECT id FROM chat_messages WHERE session_id = ? AND role = 'assistant'",
                (session_id,),
            ))
            if n_assistant == 1:
                try:
                    t = (
                        await ai_service.complete(
                            ai_service.build_title_messages(first_user, text), cfg
                        )
                    ).strip().strip('"')[:60]
                    if t:
                        db.conn.execute(
                            "UPDATE chat_sessions SET title = ? WHERE id = ?",
                            (t, session_id),
                        )
                        db.conn.commit()
                        yield "data: " + json.dumps({"title": t}, ensure_ascii=False) + "\n\n"
                except Exception:
                    pass  # 标题生成失败不影响对话
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
