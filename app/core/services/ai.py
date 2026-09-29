from __future__ import annotations

import httpx

SYSTEM_PROMPT = (
    "你是网页正文整理助手。把用户提供的网页文本整理成干净、连贯的文章 Markdown："
    "保留标题层级、段落与列表；去除导航、广告、页脚、脚本残留等无关内容；"
    "不要添加原文没有的信息；只输出 Markdown 正文。"
)


async def generate_markdown(ai_cfg: dict, text: str) -> str:
    base = (ai_cfg.get("base_url") or "").rstrip("/")
    api_key = ai_cfg.get("api_key") or ""
    model = ai_cfg.get("model") or ""
    if not base or not api_key or not model:
        raise RuntimeError("AI service is not configured")
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:12000]},
        ],
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("AI returned empty markdown")
    return content.strip()
