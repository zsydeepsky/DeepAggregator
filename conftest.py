import hashlib

import numpy as np
import pytest

from app.core.db import Database


class FakeEmbedder:
    name = "fake-embed"

    async def embed(self, texts, is_query=False):
        vecs = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vec = np.frombuffer(digest, dtype=np.uint8).astype(np.float32)[:32]
            vec = vec - vec.mean()
            norm = float(np.linalg.norm(vec))
            vecs.append(vec / norm if norm else vec)
        return np.stack(vecs)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.init()
    return database
