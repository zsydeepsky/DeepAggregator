"""内置翻译：三档 provider + 多引擎/多协议适配。

- none：关闭（用户自用浏览器翻译）
- cloud：云端免费翻译（无需 key）：google gtx / 必应 web 端点
- api：自定义接入，接口类型：
    openai  — OpenAI 兼容 LLM 翻译（本地或远程推理服务均可）
    deepl   — DeepL API（key 以 :fx 结尾为免费版）
    baidu   — 百度翻译开放平台（appid + secret，MD5 签名）
    tencent — 腾讯云机器翻译 TMT（TC3-HMAC-SHA256 签名）
    volc    — 火山翻译（火山引擎 AK/SK HMAC-SHA256 签名）
    aliyun  — 阿里云机器翻译（RPC HMAC-SHA1 签名）

配置存于 settings.translate 段（设置页「翻译」可改，带测试连接）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/translate")

GTX = "https://translate.googleapis.com/translate_a/single"
CHUNK = 1400

LANG_MAP = {
    "zh": "zh-CN",
    "zh-TW": "zh-TW",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "ru": "ru",
}
# 各家目标语言代码
LANG_TENCENT = {"zh": "zh", "zh-TW": "zh-TW", "en": "en", "ja": "ja", "ko": "ko",
                "fr": "fr", "de": "de", "es": "es", "ru": "ru"}
LANG_DEEPL = {"zh": "ZH", "zh-TW": "ZH", "en": "EN-US", "ja": "JA", "ko": "KO",
              "fr": "FR", "de": "DE", "es": "ES", "ru": "RU"}
LANG_VOLC = {"zh": "zh", "zh-TW": "zh", "en": "en", "ja": "ja", "ko": "ko",
             "fr": "fr", "de": "de", "es": "es", "ru": "ru"}
LANG_ALIYUN = {"zh": "zh", "zh-TW": "zh-tw", "en": "en", "ja": "ja", "ko": "ko",
               "fr": "fr", "de": "de", "es": "es", "ru": "ru"}
LANG_NAMES = {
    "zh": "简体中文", "zh-TW": "繁體中文", "en": "English", "ja": "日本語",
    "ko": "한국어", "fr": "Français", "de": "Deutsch", "es": "Español", "ru": "Русский",
}


class TranslateIn(BaseModel):
    texts: list[str] = Field(max_length=60)
    target: str = "zh"


def _settings(request: Request) -> dict:
    return request.app.state.core.app_settings.data.get("translate") or {}


def _chunks(text: str, limit: int = CHUNK) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for part in text.split("。"):
        seg = part + "。"
        if len(buf) + len(seg) > limit and buf:
            out.append(buf)
            buf = seg
        else:
            buf += seg
    if buf.strip():
        out.append(buf)
    return out or [text]


# ---------- cloud: google gtx ----------


async def _gtx(client: httpx.AsyncClient, text: str, tl: str) -> str:
    r = await client.get(
        GTX,
        params={"client": "gtx", "sl": "auto", "tl": tl, "dt": "t", "q": text},
    )
    r.raise_for_status()
    data = r.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


async def translate_google(client: httpx.AsyncClient, text: str, tl: str) -> str:
    parts = _chunks(text)
    outs = await asyncio.gather(*[_gtx(client, p, tl) for p in parts])
    return "".join(outs)


# ---------- cloud: bing web endpoint ----------


async def translate_mymemory(client: httpx.AsyncClient, text: str, tl: str) -> str:
    # MyMemory：匿名 500 字符/请求（超长分块）、5 万字符/天；langpair 支持 Autodetect 源
    parts = _chunks(text, 450)
    outs = []
    for p in parts:
        r = await client.get(
            "https://api.mymemory.translated.net/get",
            params={"q": p, "langpair": f"Autodetect|{tl}"},
        )
        r.raise_for_status()
        data = r.json()
        st = data.get("responseStatus")
        txt = (data.get("responseData") or {}).get("translatedText") or ""
        if st not in (200, "200") or not txt:
            raise RuntimeError(f"MyMemory 错误 {st}: {txt or data.get('responseDetails')}")
        outs.append(txt)
    return "".join(outs)


# ---------- api adapters ----------


async def api_openai(client: httpx.AsyncClient, cfg: dict, text: str, name: str) -> str:
    url = (cfg.get("api_url") or "").rstrip("/")
    if not url:
        raise RuntimeError("未配置 API 端点")
    headers = {}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    r = await client.post(
        f"{url}/chat/completions",
        headers=headers,
        json={
            "model": cfg.get("api_model") or "translation",
            "messages": [
                {"role": "system", "content": f"你是翻译引擎。把用户文本翻译成{name}，只输出译文，不要解释。"},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
        },
    )
    r.raise_for_status()
    return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


async def api_deepl(client: httpx.AsyncClient, cfg: dict, text: str, tl_code: str) -> str:
    key = cfg.get("api_key") or cfg.get("api_secret") or ""
    if not key:
        raise RuntimeError("未配置 DeepL 鉴权 Key")
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    r = await client.post(
        f"{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        json={"text": [text], "target_lang": tl_code},
    )
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


def _baidu_sign(appid: str, q: str, salt: str, secret: str) -> str:
    raw = (appid + q + salt + secret).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


async def api_baidu(client: httpx.AsyncClient, cfg: dict, text: str, tl_code: str) -> str:
    appid = cfg.get("api_id") or ""
    secret = cfg.get("api_key") or ""
    if not appid or not secret:
        raise RuntimeError("未配置百度翻译 appid/secret")
    salt = str(uuid.uuid4())
    r = await client.post(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data={
            "q": text, "from": "auto", "to": tl_code,
            "appid": appid, "salt": salt, "sign": _baidu_sign(appid, text, salt, secret),
        },
    )
    r.raise_for_status()
    data = r.json()
    if "trans_result" not in data:
        raise RuntimeError(f"百度错误 {data.get('error_code')}: {data.get('error_msg')}")
    return "\n".join(x["dst"] for x in data["trans_result"])


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


async def api_tencent(client: httpx.AsyncClient, cfg: dict, text: str, tl_code: str) -> str:
    sid = cfg.get("api_id") or ""
    skey = cfg.get("api_key") or ""
    if not sid or not skey:
        raise RuntimeError("未配置腾讯云 SecretId/SecretKey")
    ts = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    payload = json.dumps(
        {"SourceText": text, "Source": "auto", "Target": tl_code, "ProjectId": 0},
        ensure_ascii=False, separators=(",", ":"),
    )
    host = "tmt.tencentcloudapi.com"
    ctype = "application/json; charset=utf-8"
    canon = (
        "POST\n/\n\n" + f"content-type:{ctype}\nhost:{host}\n\n" +
        "content-type;host\n" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )
    sts = f"TC3-HMAC-SHA256\n{ts}\n{date}/tmt/tc3_request\n" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
    k = _hmac(("TC3" + skey).encode("utf-8"), date)
    k = _hmac(k, "tmt")
    k = _hmac(k, "tc3_request")
    sig = hmac.new(k, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    r = await client.post(
        f"https://{host}",
        json=json.loads(payload),
        headers={
            "Content-Type": ctype,
            "X-TC-Action": "TextTranslate",
            "X-TC-Version": "2018-03-21",
            "X-TC-Timestamp": str(ts),
            "X-TC-ProjectId": "0",
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={sid}/{date}/tmt/tc3_request, "
                f"SignedHeaders=content-type;host, Signature={sig}"
            ),
        },
    )
    r.raise_for_status()
    data = r.json().get("Response") or {}
    if "TargetText" not in data:
        raise RuntimeError(f"腾讯错误: {data.get('Error') or data}")
    return data["TargetText"]


def _volc_sign(cfg: dict, date: str, region: str, service: str, payload: bytes) -> str:
    k = _hmac((cfg.get("api_key") or "").encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "request")
    khex = k.hex()
    return khex


async def api_volc(client: httpx.AsyncClient, cfg: dict, text: str, tl_code: str) -> str:
    ak = cfg.get("api_id") or ""
    sk = cfg.get("api_key") or ""
    region = cfg.get("api_region") or "cn-north-1"
    if not ak or not sk:
        raise RuntimeError("未配置火山引擎 AccessKey/SecretKey")
    service = "translate"
    host = "translate.volcengineapi.com"
    payload = json.dumps(
        {"TargetLanguage": tl_code, "TextList": [text]},
        ensure_ascii=False, separators=(",", ":"),
    )
    ts = time.gmtime()
    xdate = time.strftime("%Y%m%dT%H%M%SZ", ts)
    date = time.strftime("%Y%m%d", ts)
    body_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canon = (
        "POST\n/\n\n" + f"content-type:application/json\nhost:{host}\n"
        + f"x-content-sha256:{body_hash}\nx-date:{xdate}\n\n"
        + "content-type;host;x-content-sha256;x-date\n" + body_hash
    )
    sts = (
        f"HMAC-SHA256\n{xdate}\n{date}/{region}/{service}/request\n"
        + hashlib.sha256(canon.encode("utf-8")).hexdigest()
    )
    k = _hmac(sk.encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "request")
    sig = hmac.new(k, sts.encode("utf-8"), hashlib.sha256).hexdigest()
    r = await client.post(
        f"https://{host}/",
        params={"Action": "TranslateText", "Version": "2020-06-01"},
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Date": xdate,
            "X-Content-Sha256": body_hash,
            "Authorization": (
                f"HMAC-SHA256 Credential={ak}/{date}/{region}/{service}/request, "
                f"SignedHeaders=content-type;host;x-content-sha256;x-date, Signature={sig}"
            ),
        },
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("TranslationList") or []
    if not items:
        raise RuntimeError(f"火山错误: {data}")
    return items[0].get("Translation") or ""


def _aliyun_sign(cfg: dict, params: dict) -> str:
    sorted_items = sorted(params.items())
    qs = "&".join(
        _ali_enc(k) + "=" + _ali_enc(v) for k, v in sorted_items
    )
    to_sign = "POST&%2F&" + _ali_enc(qs)
    key = (cfg.get("api_key") or "").encode("utf-8")
    return base64.b64encode(hmac.new(key, to_sign.encode("utf-8"), hashlib.sha1).digest()).decode()


def _ali_enc(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote(s, safe="-_.~")


async def api_aliyun(client: httpx.AsyncClient, cfg: dict, text: str, tl_code: str) -> str:
    ak = cfg.get("api_id") or ""
    sk = cfg.get("api_key") or ""
    region = cfg.get("api_region") or "cn-hangzhou"
    if not ak or not sk:
        raise RuntimeError("未配置阿里云 AccessKey/Secret")
    params = {
        "Action": "TranslateGeneral",
        "Version": "2018-04-08",
        "Format": "JSON",
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "SourceLanguageCode": "auto",
        "TargetLanguageCode": tl_code,
        "SourceText": text,
        "Scene": "general",
    }
    params["Signature"] = _aliyun_sign(cfg, params)
    r = await client.post(f"https://mt.{region}.aliyuncs.com/", data=params)
    r.raise_for_status()
    data = r.json()
    body = data.get("Data") or {}
    if "Translated" not in body:
        raise RuntimeError(f"阿里错误: {data.get('Code') or data.get('Message') or data}")
    return body["Translated"]


API_ADAPTERS = {
    "openai": (api_openai, None),
    "deepl": (api_deepl, LANG_DEEPL),
    "baidu": (api_baidu, LANG_MAP),
    "tencent": (api_tencent, LANG_TENCENT),
    "volc": (api_volc, LANG_VOLC),
    "aliyun": (api_aliyun, LANG_ALIYUN),
}

CLOUD_ENGINES = {"google", "mymemory"}


# ---------- 路由 ----------


@router.post("")
async def translate(request: Request, body: TranslateIn):
    cfg = _settings(request)
    provider = cfg.get("provider") or "none"
    if provider == "none":
        raise HTTPException(status_code=409, detail="翻译功能未开启")
    texts = [t.strip() for t in body.texts if t and t.strip()]
    if not texts:
        return {"translations": [t.strip() for t in body.texts]}

    if provider == "api":
        atype = cfg.get("api_type") or "openai"
        adapter, lang_map = API_ADAPTERS.get(atype, (None, None))
        if adapter is None:
            raise HTTPException(status_code=422, detail=f"未知接口类型：{atype}")
        tl = (lang_map or LANG_MAP).get(body.target, body.target)
        sem = asyncio.Semaphore(4)

        async def run(t: str) -> str:
            async with sem:
                async with httpx.AsyncClient(timeout=60) as client:
                    return await adapter(client, cfg, t, tl)

        try:
            translations = await asyncio.gather(*[run(t) for t in texts])
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"翻译 API 失败：{e}") from e
        return {"translations": translations}

    # cloud
    engine = cfg.get("cloud_engine") or "google"
    if engine not in CLOUD_ENGINES:
        engine = "google"
    tl = LANG_MAP.get(body.target, "zh-CN")
    sem = asyncio.Semaphore(6)

    async def run_c(t: str) -> str:
        async with sem:
            async with httpx.AsyncClient(timeout=25) as client:
                fn = translate_google if engine == "google" else translate_mymemory
                return await fn(client, t, tl if engine == "google" else "zh-CN" if body.target == "zh" else body.target)

    try:
        translations = await asyncio.gather(*[run_c(t) for t in texts])
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"翻译服务不可用：{e}") from e
    return {"translations": translations}


@router.post("/test")
async def translate_test(request: Request, body: dict):
    """设置页「测试连接」：用当前表单配置翻译一句样例。"""
    cfg = body.get("cfg") or {}
    provider = cfg.get("provider") or "none"
    sample = "Hello, world. This is a translation test."
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if provider == "cloud":
                engine = cfg.get("cloud_engine") or "google"
                if engine == "mymemory":
                    return {"ok": True, "text": await translate_mymemory(client, sample, "zh-CN")}
                return {"ok": True, "text": await translate_google(client, sample, "zh-CN")}
            if provider == "api":
                atype = cfg.get("api_type") or "openai"
                adapter, lang_map = API_ADAPTERS.get(atype, (None, None))
                if adapter is None:
                    return {"ok": False, "error": f"未知接口类型：{atype}"}
                tl = (lang_map or LANG_MAP).get("zh", "zh")
                text = await adapter(client, cfg, sample, tl)
                return {"ok": True, "text": text}
        return {"ok": False, "error": "未知的提供方式"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
