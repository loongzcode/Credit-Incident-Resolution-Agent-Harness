"""Configurable embedding infrastructure; domain packages never import this SDK."""
import os
from credit_harness.retrieval.embedding import normalize
from credit_harness.retrieval.models import RetrievalError, EMBEDDING_CONTRACT_VERSION


class OpenAIEmbeddingProvider:
    provider = "openai"
    embedding_contract_version = EMBEDDING_CONTRACT_VERSION

    def __init__(self, *, model_id=None, dimension=None, client=None):
        self.model_id = model_id or os.environ.get("EMBEDDING_MODEL")
        self.dimension = dimension or int(os.environ.get("EMBEDDING_DIMENSION", "0"))
        if not self.model_id or not 1 <= self.dimension <= 2000:
            raise RetrievalError("embedding model and dimension must be configured")
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30., max_retries=0)
        self._client = client

    def embed_documents(self, texts):
        try:
            if not texts or len(texts) > 128 or any(not t or len(t) > 16000 for t in texts):
                raise RetrievalError("embedding input budget exceeded")
            result = self._client.embeddings.create(model=self.model_id, dimensions=self.dimension,
                input=texts, encoding_format="float")
            rows = sorted(result.data, key=lambda r: r.index)
            if [r.index for r in rows] != list(range(len(texts))) or result.model != self.model_id:
                raise RetrievalError("embedding response contract mismatch")
            return [normalize(r.embedding, self.dimension) for r in rows]
        except Exception:
            raise RetrievalError("embedding provider unavailable") from None

    def embed_query(self, text):
        return self.embed_documents([text])[0]
