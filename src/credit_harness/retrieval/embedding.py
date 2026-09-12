import hashlib
import math
import re
from typing import Protocol
from .models import RetrievalError, EMBEDDING_CONTRACT_VERSION


class EmbeddingProvider(Protocol):
    provider: str
    model_id: str
    dimension: int
    embedding_contract_version: str
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def normalize(vector, dimension):
    if len(vector) != dimension or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector):
        raise RetrievalError("invalid embedding shape")
    norm = math.sqrt(sum(v*v for v in vector))
    if not norm or not math.isfinite(norm):
        raise RetrievalError("invalid embedding norm")
    return [float(v/norm) for v in vector]


class DeterministicFakeEmbeddingProvider:
    """Hashed tokens for offline engineering checks, NOT semantic quality proof."""
    provider = "deterministic-fake"
    model_id = "hashed-vocabulary-1"
    embedding_contract_version = EMBEDDING_CONTRACT_VERSION

    def __init__(self, dimension=64):
        self.dimension = dimension

    def embed_query(self, text):
        vector = [0.] * self.dimension
        for word in re.findall(r"[a-z]+|\d+", text.lower()):
            hashed = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(hashed[:4], "big") % self.dimension] += 1 if hashed[4] % 2 else -1
        return normalize(vector, self.dimension)

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]
