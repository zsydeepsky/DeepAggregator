import asyncio
import json
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import settings
from app.core.services import source_health
from app.core.sources.bilibili import qr_generate, qr_poll

bearer = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api")

SSE_KEEPALIVE_SECONDS = 15


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    token: str = "",
) -> None:
    # EventSource 无法携带 Header，允许 ?token= 查询参数作为 Bearer 的替代
    if settings.auth_token:
        supplied = credentials.credentials if credentials else token
        if supplied != settings.auth_token:
            raise HTTPException(status_code=401, detail="unauthorized")


router.dependencies.append(Depends(require_auth))


class SourceIn(BaseModel):
    type: str = Field(pattern="^(rss|arxiv|reddit|youtube|bilibili)$")
    name: str
    url: str = ""
    fetch_interval_min: int = 60
    config: dict = {}
    color: str = ""
    archive_enabled: bool = True
    archive_markdown: bool = False
    allow_upload: bool = False


class SourcePatch(BaseModel):
    name: str | None = None
    url: str | None = None
    fetch_interval_min: int | None = None
    config: dict | None = None
    enabled: bool | None = None
    color: str | None = None
    archive_enabled: bool | None = None
    archive_markdown: bool | None = None
    allow_upload: bool | None = None


class ItemPatch(BaseModel):
    is_read: bool | None = None
    is_starred: bool | None = None
    is_bookmarked: bool | None = None


class WorkspaceIn(BaseModel):
    name: str


class SourcePrefsIn(BaseModel):
    group_order: list[str] = []
    source_order: list[int] = []


def _core(request: Request):
    return request.app.state.core


@router.get("/source-prefs")
def get_source_prefs(request: Request):
    return _core(request).get_source_prefs()


@router.put("/source-prefs")
def put_source_prefs(request: Request, body: SourcePrefsIn):
    _core(request).set_source_prefs(body.group_order, body.source_order)
    return {"ok": True}


@router.get("/workspaces")
def list_workspaces(request: Request):
    return _core(request).manager.summaries()


@router.post("/workspaces", status_code=201)
def create_workspace(request: Request, body: WorkspaceIn):
    name = body.name.strip()
    try:
        ws = _core(request).manager.create(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="workspace already exists") from exc
    return {"name": ws.name, "active": False}


@router.post("/workspaces/{name}/activate")
def activate_workspace(request: Request, name: str):
    try:
        ws = _core(request).manager.activate(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return {"name": ws.name, "active": True}


@router.get("/settings")
def get_app_settings(request: Request):
    return _core(request).settings_view()


@router.put("/settings")
def update_app_settings(request: Request, body: dict):
    embedding = body.get("embedding") or {}
    model = str(embedding.get("model", "")).strip()
    if model and "qwen3-embedding-0.6b" not in model.lower():
        raise HTTPException(
            status_code=422,
            detail="embedding model must be Qwen3-Embedding-0.6B",
        )
    if embedding.get("provider") not in (None, "local", "api"):
        raise HTTPException(status_code=422, detail="embedding provider must be local or api")
    if embedding.get("device") not in (None, "cpu", "directml", "cuda"):
        raise HTTPException(status_code=422, detail="unsupported device")
    try:
        view = _core(request).apply_settings(body)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 用户更新了凭据段：清除对应的凭据问题登记，等待下轮抓取重新验证
    for section in ("reddit", "bilibili"):
        if body.get(section):
            source_health.clear(section)
    return view


@router.get("/sources")
def list_sources(request: Request):
    return _core(request).list_sources()


@router.post("/sources", status_code=201)
def create_source(request: Request, body: SourceIn):
    if body.type == "rss" and not body.url.strip():
        raise HTTPException(status_code=422, detail="rss source requires url")
    if body.type == "youtube":
        channel = (body.config.get("channel") or body.config.get("channel_id") or body.url).strip()
        if not channel:
            raise HTTPException(
                status_code=422, detail="youtube source requires channel url or id"
            )
    if body.type == "bilibili":
        channel = (body.config.get("channel") or body.config.get("mid") or body.url).strip()
        if not channel:
            raise HTTPException(
                status_code=422, detail="bilibili source requires space url or mid"
            )
    if body.color and not re.fullmatch(r"#[0-9a-fA-F]{6}", body.color):
        raise HTTPException(status_code=422, detail="color must be #RRGGBB or empty")
    return _core(request).create_source(
        body.type,
        body.name,
        body.url.strip(),
        body.fetch_interval_min,
        body.config,
        body.color,
        body.archive_enabled,
        body.archive_markdown,
        body.allow_upload,
    )


@router.patch("/sources/{source_id}")
def update_source(request: Request, source_id: int, body: SourcePatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "color" in fields and fields["color"] and not re.fullmatch(
        r"#[0-9a-fA-F]{6}", fields["color"]
    ):
        raise HTTPException(status_code=422, detail="color must be #RRGGBB or empty")
    source = _core(request).update_source(source_id, fields)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(request: Request, source_id: int):
    _core(request).delete_source(source_id)


@router.post("/sources/{source_id}/refresh")
async def refresh_source(request: Request, source_id: int):
    result = await _core(request).refresh_source(source_id)
    if result is None:
        raise HTTPException(status_code=404, detail="source not found")
    return result


@router.get("/items")
async def list_items(
    request: Request,
    q: str = "",
    mode: str = "semantic",
    source_id: int | None = None,
    unread: bool = False,
    starred: bool = False,
    bookmarked: bool = False,
    type: str = "",
    source_ids: str = "",
    types: str = "",
    limit: int = 50,
    offset: int = 0,
):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if mode not in {"hybrid", "keyword", "semantic"}:
        mode = "semantic"
    return await _core(request).search_items(
        q, mode, source_id, unread, starred, limit, offset, type, bookmarked, source_ids, types
    )


@router.get("/items/{item_id}")
def get_item(request: Request, item_id: int):
    item = _core(request).get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


@router.patch("/items/{item_id}")
def patch_item(request: Request, item_id: int, body: ItemPatch):
    item = _core(request).set_item_flags(item_id, body.is_read, body.is_starred, body.is_bookmarked)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


class MarkReadIn(BaseModel):
    source_id: int | None = None
    type: str | None = None


@router.post("/items/mark-read")
def mark_read_bulk(request: Request, body: MarkReadIn):
    """长按充能的批量已读：按单源或按整个源类型。"""
    if not body.source_id and not body.type:
        raise HTTPException(status_code=422, detail="需指定 source_id 或 type")
    n = _core(request).mark_read_bulk(body.source_id, body.type)
    return {"marked": n}


@router.post("/items/{item_id}/archive")
def archive_item(request: Request, item_id: int):
    return _core(request).request_archive(item_id)


@router.delete("/items/{item_id}/archive")
async def delete_archive(request: Request, item_id: int):
    # async def：delete_archive 会触发队列事件广播与历史清理，须在事件循环线程执行
    try:
        return _core(request).delete_archive(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="item not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/items/{item_id}/archive/upload")
async def upload_archive(request: Request, item_id: int, filename: str = ""):
    """用户上传本地文件作为该条目的存档（需源开启 allow_upload）。

    请求体为原始文件字节（不走 multipart，避免 python-multipart 依赖），
    文件名经 ?filename= 查询参数传递；流式落盘并在超限时截断为 413。
    """
    core = _core(request)
    limit = core.settings.archive_max_bytes
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename or "upload").suffix) as tmp:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                tmp.close()
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过大小上限（{limit // (1024 * 1024)} MB）",
                )
            tmp.write(chunk)
        tmp_path = Path(tmp.name)
    try:
        return core.upload_archive(
            item_id, filename, request.headers.get("content-type", ""), tmp_path
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="item not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/archive/queue")
async def archive_queue(request: Request):
    # async def：snapshot 遍历队列内存结构，与执行循环同线程避免并发迭代
    return _core(request).queue_snapshot()


@router.get("/events")
async def events(request: Request):
    queue = _core(request).queue.bus
    stream: asyncio.Queue = queue.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(
                        stream.get(), timeout=SSE_KEEPALIVE_SECONDS
                    )
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            queue.unsubscribe(stream)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/assets/{asset_id}/file")
def asset_file(request: Request, asset_id: int):
    resolved = _core(request).asset_file(asset_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="asset not found")
    path, mime, name = resolved
    return FileResponse(
        path, media_type=mime, filename=name, content_disposition_type="inline"
    )


@router.post("/bilibili/login/qr")
async def bilibili_qr_generate():
    try:
        return await qr_generate()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"B站登录二维码获取失败：{exc}") from exc


@router.get("/bilibili/login/qr/poll")
async def bilibili_qr_poll(request: Request, key: str):
    try:
        result = await qr_poll(key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"B站登录状态轮询失败：{exc}") from exc
    if result.get("code") == 0:
        cookies = result.get("cookies") or {}
        _core(request).app_settings.update(
            {"bilibili": {k: v for k, v in cookies.items() if v}}
        )
        source_health.clear("bilibili")
    return result


@router.get("/credential-issues")
def credential_issues(request: Request):
    return {"issues": source_health.snapshot()}


@router.get("/stats")
def stats(request: Request):
    return _core(request).stats()
