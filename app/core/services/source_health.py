"""源抓取的凭据问题登记表（内存态）：顶栏 ❌ 提示按钮的数据源。

判定规则按源类型维护关键字；抓取失败时由 ingest.fetch_source 调用 report，
成功或用户更新凭据后 clear。
"""

import time

ISSUES: dict[str, dict] = {}

_MARKERS: dict[str, tuple[str, ...]] = {
    "reddit": ("credentials", "unauthorized", "401", "token"),
    "bilibili": ("-352", "风控", "sessdata", "登录", "credential"),
}


def is_credential_error(source_type: str, message: str) -> bool:
    msg = (message or "").lower()
    return any(marker in msg for marker in _MARKERS.get(source_type, ()))


def report(source_type: str, source_name: str, error: str) -> None:
    entry = ISSUES.setdefault(source_type, {"sources": [], "error": "", "at": ""})
    if source_name not in entry["sources"]:
        entry["sources"].append(source_name)
    entry["error"] = error
    entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def clear(source_type: str) -> None:
    ISSUES.pop(source_type, None)


def snapshot() -> list[dict]:
    return [
        {"type": t, "sources": v["sources"], "error": v["error"], "at": v["at"]}
        for t, v in ISSUES.items()
    ]
