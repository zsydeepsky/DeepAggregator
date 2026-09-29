from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np


QUERY_INSTRUCTION_TEMPLATE = "Instruct: {task}\nQuery: {text}"
DEFAULT_TASK = "Given a query, retrieve relevant documents that answer the query"


class BaseEmbedder:
    name: str = ""

    async def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        raise NotImplementedError


def _normalize(vecs: np.ndarray) -> np.ndarray:
    vecs = vecs.astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


class OnnxEmbedder(BaseEmbedder):
    def __init__(
        self,
        name: str,
        local_path: str = "",
        hub_id: str = "",
        max_length: int = 1024,
        device: str = "cpu",
    ):
        self.name = name
        self._local_path = local_path
        self._hub_id = hub_id
        self._max_length = max_length
        self._device = device or "cpu"
        self._session = None
        self._tokenizer = None
        self._input_names = None

    def _resolve_path(self) -> str:
        if self._local_path and Path(self._local_path).exists():
            return self._local_path
        from huggingface_hub import snapshot_download

        return snapshot_download(
            self._hub_id,
            allow_patterns=[
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "onnx/model_quantized.onnx",
            ],
        )

    def _load(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from transformers import AutoTokenizer

        path = self._resolve_path()
        self._tokenizer = AutoTokenizer.from_pretrained(path, padding_side="left")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = self._resolve_providers(ort, self._device)
        try:
            self._session = ort.InferenceSession(
                str(Path(path) / "onnx" / "model_quantized.onnx"),
                options,
                providers=providers,
            )
        except Exception:
            if providers != ["CPUExecutionProvider"]:
                self._session = ort.InferenceSession(
                    str(Path(path) / "onnx" / "model_quantized.onnx"),
                    options,
                    providers=["CPUExecutionProvider"],
                )
            else:
                raise
        self._input_names = [item.name for item in self._session.get_inputs()]

    @staticmethod
    def _resolve_providers(ort, device: str) -> list[str]:
        wanted = {
            "directml": ["DmlExecutionProvider", "CPUExecutionProvider"],
            "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        }.get(device)
        if not wanted:
            return ["CPUExecutionProvider"]
        available = ort.get_available_providers()
        usable = [p for p in wanted if p in available]
        return usable or ["CPUExecutionProvider"]

    def _embed_one(self, text: str) -> np.ndarray:
        encoded = self._tokenizer(
            text,
            padding=True,
            truncation=True,
            max_length=self._max_length,
            return_tensors="np",
        )
        feeds = {name: encoded[name] for name in self._input_names}
        output = np.asarray(self._session.run(None, feeds)[0])
        mask = encoded["attention_mask"].astype(bool)
        hidden = np.where(mask[..., None], output, 0.0)
        if mask[:, -1].all():
            pooled = hidden[:, -1, :]
        else:
            last_idx = mask.sum(axis=1) - 1
            pooled = hidden[np.arange(hidden.shape[0]), last_idx]
        return pooled[0].astype(np.float32)

    def _encode_sync(self, texts: list[str], is_query: bool) -> np.ndarray:
        self._load()
        if is_query:
            texts = [
                QUERY_INSTRUCTION_TEMPLATE.format(task=DEFAULT_TASK, text=text)
                for text in texts
            ]
        vecs = np.stack([self._embed_one(text) for text in texts])
        return _normalize(vecs)

    async def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        import anyio

        return await anyio.to_thread.run_sync(self._encode_sync, texts, is_query)


class OpenAICompatEmbedder(BaseEmbedder):
    def __init__(self, model_name: str, base_url: str, api_key: str):
        if not base_url or not api_key:
            raise RuntimeError(
                "DA_EMBEDDING_API_BASE / DA_EMBEDDING_API_KEY are required when"
                " DA_EMBEDDING_PROVIDER=api"
            )
        self.name = model_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.name, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        return _normalize(np.asarray([item["embedding"] for item in data]))


def build_embedder(cfg) -> BaseEmbedder:
    if cfg.embedding_provider == "api":
        return OpenAICompatEmbedder(
            cfg.embedding_model, cfg.embedding_api_base, cfg.embedding_api_key
        )
    return OnnxEmbedder(
        cfg.embedding_model,
        cfg.embedding_local_path,
        cfg.embedding_hub_id,
        cfg.embedding_max_length,
        getattr(cfg, "embedding_device", "cpu"),
    )


def _ensure_vec_table(conn, dim: int) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_vectors'"
    ).fetchone()
    meta = conn.execute("SELECT dim FROM vector_meta LIMIT 1").fetchone()
    if table and meta and meta["dim"] != dim:
        conn.execute("DROP TABLE IF EXISTS item_vectors")
        conn.execute("DELETE FROM vector_meta")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS item_vectors"
        f" USING vec0(item_id INTEGER PRIMARY KEY, embedding float[{dim}])"
    )


def store_vectors(db, embedder, ids: list[int], vecs: np.ndarray) -> None:
    conn = db.conn
    dim = int(vecs.shape[1])
    _ensure_vec_table(conn, dim)
    for item_id, vec in zip(ids, vecs):
        conn.execute(
            "INSERT OR REPLACE INTO item_vectors(item_id, embedding) VALUES (?, ?)",
            (item_id, vec.astype(np.float32).tobytes()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO vector_meta(item_id, model, dim) VALUES (?, ?, ?)",
            (item_id, embedder.name, dim),
        )
    conn.commit()


async def embed_missing(db, embedder, limit: int) -> int:
    rows = db.all(
        "SELECT i.id, i.title, i.summary FROM items i"
        " LEFT JOIN vector_meta v ON v.item_id = i.id AND v.model = ?"
        " WHERE v.item_id IS NULL ORDER BY i.id DESC LIMIT ?",
        (embedder.name, limit),
    )
    if not rows:
        return 0
    texts = [f"{r['title']}\n{r['summary']}".strip()[:4000] for r in rows]
    vecs = await embedder.embed(texts)
    store_vectors(db, embedder, [r["id"] for r in rows], vecs)
    return len(rows)


def knn_search(db, model: str, query_vec: np.ndarray, k: int) -> list[tuple[int, float]]:
    try:
        rows = db.all(
            "SELECT item_id, distance FROM item_vectors WHERE embedding MATCH ? AND k = ?",
            (query_vec.astype(np.float32).tobytes(), k),
        )
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    placeholders = ",".join("?" * len(rows))
    meta = {
        r["item_id"]: r["model"]
        for r in db.all(
            f"SELECT item_id, model FROM vector_meta WHERE item_id IN ({placeholders})",
            [r["item_id"] for r in rows],
        )
    }
    ordered = sorted(rows, key=lambda r: r["distance"])
    return [
        (r["item_id"], float(r["distance"]))
        for r in ordered
        if meta.get(r["item_id"]) == model
    ]
