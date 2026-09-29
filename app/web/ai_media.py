"""AI 对话图片媒体：上传（原始字节 + ?filename=）与读取。"""

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.web.chats import require_auth

router = APIRouter(prefix="/api/ai-media", dependencies=[Depends(require_auth)])

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_NAME = 120


def media_dir(request: Request) -> Path:
    d = request.app.state.core.manager.current().path / "ai_media"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/upload")
async def upload(request: Request, filename: str = "image.png"):
    name = Path(filename or "image.png").name
    name = re.sub(r"[^\w.\-一-龥]", "_", name)[-MAX_NAME:]
    ext = Path(name).suffix.lower()
    if ext and ext not in ALLOWED_EXT:
        raise HTTPException(status_code=415, detail=f"不支持的图片格式：{ext}")
    if not ext:
        name += ".png"
    data = await request.body()
    if not data:
        raise HTTPException(status_code=422, detail="空文件")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片超过 20MB 上限")
    uname = f"{uuid.uuid4().hex}{ext}"
    (media_dir(request) / uname).write_bytes(data)
    # 返回带原始名的展示名与读取 URL（读取路径用 uuid 名避免注入）
    return {"url": f"/api/ai-media/{uname}", "name": name}


@router.get("/{name}")
def get_media(request: Request, name: str):
    if not re.fullmatch(r"[0-9a-f]{32}(\.(png|jpg|jpeg|gif|webp))?", name):
        raise HTTPException(status_code=404)
    path = media_dir(request) / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)
