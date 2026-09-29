from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    data_dir: Path = Path("./data")
    workspace: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: str = ""

    embedding_provider: str = "local"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_local_path: str = ""
    embedding_hub_id: str = "n24q02m/Qwen3-Embedding-0.6B-ONNX"
    embedding_max_length: int = 1024
    embedding_device: str = "cpu"
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embed_batch_size: int = 16
    embed_poll_seconds: int = 30

    fetch_timeout: int = 30
    archive_timeout: int = 120
    archive_max_bytes: int = 200 * 1024 * 1024

    model_config = {"env_prefix": "DA_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
