import json
import os
import pytest
from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
from credit_harness.retrieval.models import RetrievalError


def test_openai_embedding_offline_roundtrip():
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"object": "list", "model": "configured-synthetic-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [1., 0., 0.]}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = openai.OpenAI(api_key="synthetic-test-key", http_client=http, max_retries=0)
        provider = OpenAIEmbeddingProvider(model_id="configured-synthetic-model", dimension=3, client=client)
        assert provider.embed_query("payment finality unknown") == [1., 0., 0.]
    assert requests == [{"model": "configured-synthetic-model", "dimensions": 3,
        "input": ["payment finality unknown"], "encoding_format": "float"}]


@pytest.mark.parametrize("response", [{}, {"data": []}, {"model": "configured-model", "data": [{"index": 0, "embedding": [0., 0.]}]}])
def test_embedding_invalid_response_fails_closed(response):
    from types import SimpleNamespace
    class Stub:
        def create(self, **kw):
            return SimpleNamespace(**response)
    provider = OpenAIEmbeddingProvider(model_id="configured-model", dimension=3, client=SimpleNamespace(embeddings=Stub()))
    with pytest.raises(RetrievalError, match="embedding provider unavailable"):
        provider.embed_query("payment unknown")


@pytest.mark.llm
def test_live_embedding_semantic_paraphrases():
    if os.getenv("RUN_EMBEDDING_TESTS") != "1" or not os.getenv("OPENAI_API_KEY"):
        pytest.skip("optional live embedding quality: RUN_EMBEDDING_TESTS, API key and embedding model configuration required")
    from scripts.benchmark_retrieval import semantic_benchmark
    report = semantic_benchmark(OpenAIEmbeddingProvider())
    assert report["paraphrase_recall_at_1"] == 1
