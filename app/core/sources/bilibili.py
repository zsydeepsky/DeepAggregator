"""Bilibili 空间源：UP 主视频投稿抓取（wbi 签名 + 登录态）。

config: {"channel": "https://space.bilibili.com/<mid> 或纯数字 mid"}
抓取：空间视频接口 /x/space/wbi/arc/search，需 wbi 签名（nav 取每日密钥 →
      固定置换表混排 32 位 → md5(排序参数+wts+key)）、buvid3/buvid4 Cookie
      （finger/spi 获取 + 主站访问激活）、空间子页 w_webid 令牌；
      未登录易触发 -352 风控，登录态（设置 → Bilibili 接入）后稳定。
条目：RawItem(guid="bili_<bvid>", image=封面, duration=视频时长秒)，feed 卡片
      与 YouTube 一致地"图左文右 + 时长徽章"。
存档：视频页快照仅作兜底，视频本体由用户「上传存档」提供（allow_upload）。
viewer：优先上传的 video/image 资产，html 快照殿后。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import httpx

from app.core.sources.base import ArchiveRequest, BaseSource, RawItem, http_get_text

log = logging.getLogger("deepaggregator.bilibili")

# B站 api.bilibili.com 对非浏览器 UA 直接 412，需以浏览器身份请求
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_MID = re.compile(r"\d{4,}")
_PAGE_MID = re.compile(r'"mid":(\d+)')
_LENGTH = re.compile(r"(\d{1,2}):(?:(\d{1,2}):)?(\d{2})")
_W_WEBID = re.compile(r'"w_webid":"([^"]+)"')

# wbi 混排置换表（bilibili-API-collect 公开算法，密钥轮换但置换表固定）
_WBI_MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

WBI_CACHE: dict[str, tuple[str, float]] = {"key": ("", 0)}
BUVID_CACHE: dict[str, tuple[str, float]] = {"buvid": ("", 0)}
QR_STATE: dict[str, float] = {}
CACHE_TTL = 3600


def _headers(referer: str, buvid3: str = "", buvid4: str = "", sessdata: str = "",
             bili_jct: str = "", dedeuserid: str = "") -> dict:
    h = {"User-Agent": BROWSER_UA, "Referer": referer}
    cookie = ""
    if buvid3:
        cookie = f"buvid3={buvid3}; b_nut={int(time.time())}"
        if buvid4:
            cookie += f"; buvid4={buvid4}"
    login = ""
    if sessdata:
        login += f"SESSDATA={sessdata}"
    if bili_jct:
        login += f"; bili_jct={bili_jct}"
    if dedeuserid:
        login += f"; DedeUserID={dedeuserid}"
    if login:
        cookie = (cookie + "; " + login) if cookie else login
    if cookie:
        h["Cookie"] = cookie
    return h


def mixin_wbi_key(combined: str) -> str:
    """对 img_key+sub_key（各 32 位）应用固定置换表并截取 32 位。"""
    return "".join(combined[i] for i in _WBI_MIXIN_TABLE if i < len(combined))[:32]


def parse_length(text: str) -> int:
    """"MM:SS" / "HH:MM:SS" → 秒。"""
    m = _LENGTH.match((text or "").strip())
    if not m:
        return 0
    parts = [int(p) for p in m.groups() if p is not None]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def resolve_mid(channel: str, page_html: str = "") -> str:
    """从空间链接 / 纯数字 / 页面 HTML 中提取 UP 主 mid。"""
    m = _MID.search(channel or "")
    if m:
        return m.group(0)
    m = _PAGE_MID.search(page_html or "")
    if m:
        return m.group(1)
    raise ValueError(f"cannot resolve bilibili mid from {channel!r}")


def wbi_sign(params: dict, mixin_key: str) -> dict:
    """对查询参数做 wbi 签名，返回含 wts/w_rid 的最终参数。"""
    signed = {**params, "wts": int(time.time())}
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in sorted(signed.items())
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def parse_space_videos(payload: dict) -> list[RawItem]:
    """解析 /x/space/wbi/arc/search 响应的 vlist 为 RawItem 列表。"""
    if payload.get("code") != 0:
        raise RuntimeError(f"bilibili api error: {payload.get('code')} {payload.get('message', '')}")
    vlist = ((payload.get("data") or {}).get("list") or {}).get("vlist") or []
    items: list[RawItem] = []
    for d in vlist:
        bvid = d.get("bvid") or ""
        title = (d.get("title") or "").strip()
        if not bvid or not title:
            continue
        description = (d.get("description") or "").strip()
        if len(description) > 400:
            description = description[:400] + "..."
        items.append(
            RawItem(
                guid=f"bili_{bvid}",
                title=title,
                summary=description or "(视频)",
                url=f"https://www.bilibili.com/video/{bvid}",
                author=d.get("author") or "",
                published_at=datetime.fromtimestamp(
                    int(d.get("created") or 0), tz=timezone.utc
                ),
                image=(d.get("pic") or "").replace("http://", "https://"),
                duration=parse_length(d.get("length") or ""),
            )
        )
    return items


async def qr_generate() -> dict:
    """生成 B站扫码登录二维码（官方 passport 接口），返回 {url, qrcode_key}。"""
    html = await http_get_text(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers=_headers("https://passport.bilibili.com/"),
    )
    data = (json.loads(html) or {}).get("data") or {}
    key = data.get("qrcode_key") or ""
    QR_STATE[key] = time.time()
    return {"url": data.get("url", ""), "qrcode_key": key}


async def qr_poll(qrcode_key: str) -> dict:
    """轮询扫码结果。code: 86101 未扫码 / 86090 已扫码待确认 / 0 登录成功。

    成功时登录 Cookie（SESSDATA/bili_jct/DedeUserID）在响应 Set-Cookie 中。
    """
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        headers=_headers("https://passport.bilibili.com/"),
    ) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
        )
    payload = resp.json() or {}
    data = payload.get("data") or {}
    out = {"code": data.get("code"), "message": payload.get("message", "")}
    if data.get("code") == 0:
        cookies = {c.name: c.value for c in resp.cookies.jar}
        out["cookies"] = {
            "sessdata": cookies.get("SESSDATA", ""),
            "bili_jct": cookies.get("bili_jct", ""),
            "dedeuserid": cookies.get("DedeUserID", ""),
        }
    return out


class BilibiliSource(BaseSource):
    type = "bilibili"

    def channel_value(self) -> str:
        return (
            self.config.get("channel")
            or self.config.get("mid")
            or self.source["url"]
            or ""
        ).strip()

    def _login(self) -> tuple[str, str, str]:
        """设置中保存的登录凭据（扫码登录或手动粘贴）。"""
        if not self.app_settings:
            return "", "", ""
        b = self.app_settings.data.get("bilibili") or {}
        return (
            (b.get("sessdata") or "").strip(),
            (b.get("bili_jct") or "").strip(),
            (b.get("dedeuserid") or "").strip(),
        )

    async def _mid(self) -> str:
        value = self.channel_value()
        if not value:
            raise ValueError("bilibili source requires a space url or mid")
        direct = resolve_mid(value)
        if direct:
            return direct
        # 自定义域名/别名：抓空间页从 __INITIAL_STATE__ 提取 mid
        page_url = value if value.startswith("http") else f"https://space.bilibili.com/{value}"
        html = await http_get_text(page_url, headers=_headers(page_url))
        return resolve_mid(value, html)

    async def _wbi_key(self, buvid3: str = "", buvid4: str = "") -> str:
        cached, expires = WBI_CACHE["key"]
        if cached and expires > time.time():
            return cached
        html = await http_get_text(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=_headers("https://www.bilibili.com/", buvid3, buvid4),
        )
        wbi = (json.loads(html).get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        key = mixin_wbi_key(img + sub)
        WBI_CACHE["key"] = (key, time.time() + CACHE_TTL)
        return key

    async def _buvid(self) -> tuple[str, str]:
        """获取游客设备指纹 buvid3/buvid4 并访问主站激活（降低风控概率）。"""
        cached, expires = BUVID_CACHE["buvid"]
        if cached and expires > time.time():
            b3, b4 = cached.split("|", 1)
            return b3, b4
        html = await http_get_text(
            "https://api.bilibili.com/x/frontend/finger/spi",
            headers=_headers("https://www.bilibili.com/"),
        )
        data = json.loads(html).get("data") or {}
        b3 = data.get("b_3") or ""
        b4 = data.get("b_4") or ""
        BUVID_CACHE["buvid"] = (f"{b3}|{b4}", time.time() + CACHE_TTL)
        if b3:
            try:
                await http_get_text(
                    "https://www.bilibili.com/",
                    headers=_headers("https://www.bilibili.com/", b3, b4),
                )
            except Exception:
                pass  # 激活失败不影响后续尝试
        return b3, b4

    async def fetch(self) -> list[RawItem]:
        mid = await self._mid()
        buvid3, buvid4 = await self._buvid()
        mixin_key = await self._wbi_key(buvid3, buvid4)
        # w_webid：空间视频子页 HTML 内的防爬令牌，缺失可能触发 -352
        w_webid = ""
        try:
            subpage = await http_get_text(
                f"https://space.bilibili.com/{mid}/video",
                headers=_headers(f"https://space.bilibili.com/{mid}/", buvid3, buvid4),
            )
            m = _W_WEBID.search(subpage)
            if m:
                w_webid = m.group(1)
        except Exception:
            pass  # 拿不到也继续尝试签名请求
        params = {"mid": mid, "pn": 1, "ps": 25, "order": "pubdate"}
        if w_webid:
            params["w_webid"] = w_webid
        params = wbi_sign(params, mixin_key)
        query = urllib.parse.urlencode(params)
        sessdata, bili_jct, dedeuserid = self._login()
        cookie = f"buvid3={buvid3}; buvid4={buvid4}; b_nut={int(time.time())}"
        if sessdata:
            cookie += f"; SESSDATA={sessdata}"
        if bili_jct:
            cookie += f"; bili_jct={bili_jct}"
        if dedeuserid:
            cookie += f"; DedeUserID={dedeuserid}"
        html = await http_get_text(
            f"https://api.bilibili.com/x/space/wbi/arc/search?{query}",
            headers=_headers(f"https://space.bilibili.com/{mid}/", sessdata=sessdata,
                             bili_jct=bili_jct, dedeuserid=dedeuserid) | {"Cookie": cookie},
        )
        return parse_space_videos(json.loads(html))

    async def archive_requests(self, item: dict) -> list[ArchiveRequest]:
        # 视频本体无法直接抓取；视频页快照仅作兜底，正式内容由用户「上传存档」提供
        return [ArchiveRequest(url=item.get("url") or "", kind="html")]

    def viewer(self, item: dict, assets: list[dict]) -> dict:
        by_kind: dict[str, dict] = {}
        for asset in reversed(assets):
            by_kind.setdefault(asset["kind"], asset)
        for kind in ("video", "image", "pdf", "json", "md", "file", "html"):
            if kind in by_kind:
                return {"kind": kind, "asset": by_kind[kind]}
        return {"kind": "none"}
