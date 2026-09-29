import copy

import json
from pathlib import Path


def _default_settings(env) -> dict:
    return {
        "ai": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
        },
        "embedding": {
            "provider": env.embedding_provider,
            "model": env.embedding_model,
            "local_path": env.embedding_local_path,
            "device": env.embedding_device,
            "api_base": env.embedding_api_base,
            "api_key": env.embedding_api_key,
        },
        "reddit": {
            "client_id": "",
            "client_secret": "",
        },
        "bilibili": {
            "sessdata": "",
            "bili_jct": "",
            "dedeuserid": "",
        },
        "translate": {
            "provider": "none",      # none | cloud | api
            "cloud_engine": "google",  # google | bing
            "api_type": "openai",    # openai | deepl | baidu | tencent | volc | aliyun
            "api_url": "",
            "api_model": "",
            "api_key": "",           # openai key / deepl key / baidu secret / tencent&volc&ali secret
            "api_id": "",            # tencent SecretId / volc&aliyun AccessKey / baidu appid
            "api_region": "",        # volc/aliyun 地域
        },
    }


SECTIONS = ("ai", "embedding", "reddit", "bilibili", "translate")


class AppSettings:
    def __init__(self, root: Path, env):
        self.path = Path(root) / "settings.json"
        self.env = env
        self.data = _default_settings(env)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for section in SECTIONS:
            if isinstance(saved.get(section), dict):
                for key, value in saved[section].items():
                    if key in self.data[section]:
                        self.data[section][key] = value

    def view(self) -> dict:
        return copy.deepcopy(self.data)

    def update(self, patch: dict) -> dict:
        for section in SECTIONS:
            if isinstance(patch.get(section), dict):
                for key, value in patch[section].items():
                    if key in self.data[section]:
                        self.data[section][key] = value
        self.save()
        return self.view()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def merged(self):
        merged = copy.copy(self.env)
        embedding = self.data["embedding"]
        for key, attr in (
            ("provider", "embedding_provider"),
            ("model", "embedding_model"),
            ("local_path", "embedding_local_path"),
            ("device", "embedding_device"),
            ("api_base", "embedding_api_base"),
            ("api_key", "embedding_api_key"),
        ):
            if embedding.get(key):
                setattr(merged, attr, embedding[key])
        return merged
