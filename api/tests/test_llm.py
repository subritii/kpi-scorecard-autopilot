"""Tests for the LLM provider layer. No test ever reaches a real model.

Real providers are exercised through httpx.MockTransport (an in-memory fake
server), and conftest.py makes any real httpx network call fail the test.
"""

import json
import math

import httpx
import pytest

from app.llm import get_provider
from app.llm.azure_openai import AzureOpenAIProvider
from app.llm.base import EmbeddingDimensionError, LLMConfigError, LLMError
from app.llm.fake import FakeProvider
from app.llm.ollama import OllamaProvider

DIM = 4

OLLAMA_ENV = {
    "LLM_PROVIDER": "ollama",
    "EMBEDDING_DIM": "768",
    "OLLAMA_BASE_URL": "http://ollama.test:11434",
    "OLLAMA_CHAT_MODEL": "llama3.2:3b",
    "OLLAMA_EMBED_MODEL": "nomic-embed-text",
}
AZURE_ENV = {
    "LLM_PROVIDER": "azure_openai",
    "EMBEDDING_DIM": "1536",
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.openai.azure.test",
    "AZURE_OPENAI_API_KEY": "sk-test-secret-value",
    "AZURE_OPENAI_API_VERSION": "2024-06-01",
    "AZURE_OPENAI_CHAT_DEPLOYMENT": "chat-dep",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "embed-dep",
}


def mock_client(handler) -> httpx.Client:
    """An httpx client whose requests go to `handler` instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- provider selection ---------------------------------------------------------


def test_default_provider_is_ollama():
    env = {k: v for k, v in OLLAMA_ENV.items() if k != "LLM_PROVIDER"}
    provider = get_provider(env)
    assert isinstance(provider, OllamaProvider)
    assert provider.embedding_dim == 768


def test_azure_selected_by_env():
    assert isinstance(get_provider(AZURE_ENV), AzureOpenAIProvider)


def test_fake_selected_by_env():
    provider = get_provider({"LLM_PROVIDER": "fake", "EMBEDDING_DIM": "16"})
    assert isinstance(provider, FakeProvider)
    assert provider.embedding_dim == 16


def test_unknown_provider_is_a_clear_error():
    with pytest.raises(LLMConfigError, match="LLM_PROVIDER.*azure_openai.*ollama"):
        get_provider({"LLM_PROVIDER": "gpt", "EMBEDDING_DIM": "768"})


@pytest.mark.parametrize("dim", [None, "", "abc", "0", "-5"])
def test_bad_embedding_dim_is_a_clear_error(dim):
    env = {**OLLAMA_ENV}
    if dim is None:
        del env["EMBEDDING_DIM"]
    else:
        env["EMBEDDING_DIM"] = dim
    with pytest.raises(LLMConfigError, match="EMBEDDING_DIM"):
        get_provider(env)


def test_missing_azure_settings_are_named_without_leaking_secrets():
    env = {**AZURE_ENV, "AZURE_OPENAI_ENDPOINT": "", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": ""}
    with pytest.raises(LLMConfigError) as err:
        get_provider(env)
    message = str(err.value)
    assert "AZURE_OPENAI_ENDPOINT" in message
    assert "AZURE_OPENAI_EMBEDDING_DEPLOYMENT" in message
    assert AZURE_ENV["AZURE_OPENAI_API_KEY"] not in message


# --- embedding size check (shared by every provider) ----------------------------


def test_wrong_embedding_size_fails_with_both_numbers():
    provider = FakeProvider(embedding_dim=DIM, fixed={"hello": [0.1, 0.2, 0.3]})  # 3, not 4
    with pytest.raises(EmbeddingDimensionError, match=r"3.*EMBEDDING_DIM=4"):
        provider.embed(["hello"])


def test_wrong_number_of_vectors_fails():
    class Broken(FakeProvider):
        def _embed(self, texts, purpose):
            return super()._embed(texts, purpose)[:-1]  # drops one

    with pytest.raises(LLMError, match="2 texts.*1 vectors"):
        Broken(embedding_dim=DIM).embed(["a", "b"])


def test_empty_input_returns_no_vectors():
    assert FakeProvider(embedding_dim=DIM).embed([]) == []


# --- fake provider --------------------------------------------------------------


def test_fake_embeddings_are_deterministic_and_unit_length():
    provider = FakeProvider(embedding_dim=64)
    [a1], [a2] = (
        provider.embed(["late customer payments"]),
        provider.embed(["late customer payments"]),
    )
    assert a1 == a2
    assert math.isclose(sum(x * x for x in a1), 1.0)


def test_fake_embeddings_reflect_shared_words():
    provider = FakeProvider(embedding_dim=256)
    base, similar, unrelated = provider.embed(
        [
            "customers paid late, AR over 60 days rose",
            "AR over 60 rose because two customers paid late",
            "plumbing fixtures overstock purchase",
        ]
    )

    def cos(u, v):
        return sum(x * y for x, y in zip(u, v, strict=True))

    assert cos(base, similar) > cos(base, unrelated)


def test_fake_fixed_embeddings_override():
    provider = FakeProvider(embedding_dim=DIM, fixed={"x": [1.0, 0.0, 0.0, 0.0]})
    assert provider.embed(["x"]) == [[1.0, 0.0, 0.0, 0.0]]


def test_fake_complete_returns_fixed_text_and_records_prompts():
    provider = FakeProvider(embedding_dim=DIM, completion="AR rose by 57,749.06.")
    assert provider.complete("describe AR", system="be factual") == "AR rose by 57,749.06."
    assert provider.calls == [{"prompt": "describe AR", "system": "be factual"}]


# --- Ollama (mocked HTTP) ---------------------------------------------------------


def test_ollama_embed_request_and_nomic_prefixes():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.5] * DIM for _ in body["input"]]})

    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        chat_model="llama3.2:3b",
        embed_model="nomic-embed-text",
        embedding_dim=DIM,
        client=mock_client(handler),
    )
    assert provider.embed(["AR rose"], purpose="query") == [[0.5] * DIM]
    provider.embed(["AR definition"])  # default purpose: document

    query_req, doc_req = seen
    assert query_req.url == "http://ollama.test:11434/api/embed"
    assert json.loads(query_req.content) == {
        "model": "nomic-embed-text",
        "input": ["search_query: AR rose"],
    }
    assert json.loads(doc_req.content)["input"] == ["search_document: AR definition"]


def test_ollama_other_models_get_no_prefix():
    def handler(request):
        assert json.loads(request.content)["input"] == ["plain text"]
        return httpx.Response(200, json={"embeddings": [[0.0] * DIM]})

    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        chat_model="m",
        embed_model="all-minilm",
        embedding_dim=DIM,
        client=mock_client(handler),
    )
    provider.embed(["plain text"], purpose="query")


def test_ollama_model_with_wrong_dimension_fails_clearly():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.1] * 1024]})

    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        chat_model="m",
        embed_model="mxbai-embed-large",
        embedding_dim=768,
        client=mock_client(handler),
    )
    with pytest.raises(EmbeddingDimensionError, match=r"1024.*EMBEDDING_DIM=768"):
        provider.embed(["x"])


def test_ollama_complete():
    def handler(request):
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["model"] == "llama3.2:3b"
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0
        assert body["messages"] == [
            {"role": "system", "content": "be factual"},
            {"role": "user", "content": "describe AR"},
        ]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "AR rose."}})

    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        chat_model="llama3.2:3b",
        embed_model="nomic-embed-text",
        embedding_dim=DIM,
        client=mock_client(handler),
    )
    assert provider.complete("describe AR", system="be factual") == "AR rose."


def test_ollama_http_error_is_llm_error():
    provider = OllamaProvider(
        base_url="http://ollama.test:11434",
        chat_model="m",
        embed_model="nomic-embed-text",
        embedding_dim=DIM,
        client=mock_client(lambda request: httpx.Response(500, text="model not found")),
    )
    with pytest.raises(LLMError, match="500"):
        provider.embed(["x"])


# --- Azure OpenAI (mocked HTTP) ---------------------------------------------------


def azure(handler) -> AzureOpenAIProvider:
    return AzureOpenAIProvider(
        endpoint="https://example-resource.openai.azure.test",
        api_key="sk-test-secret-value",
        api_version="2024-06-01",
        chat_deployment="chat-dep",
        embedding_deployment="embed-dep",
        embedding_dim=DIM,
        client=mock_client(handler),
    )


def test_azure_embed_request_and_order_by_index():
    def handler(request):
        assert request.url.path == "/openai/deployments/embed-dep/embeddings"
        assert request.url.params["api-version"] == "2024-06-01"
        assert request.headers["api-key"] == "sk-test-secret-value"
        assert json.loads(request.content) == {"input": ["a", "b"]}
        # The API may return items out of order; `index` says which input each is for.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 0.0, 0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                ]
            },
        )

    assert azure(handler).embed(["a", "b"]) == [[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def test_azure_complete():
    def handler(request):
        assert request.url.path == "/openai/deployments/chat-dep/chat/completions"
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["messages"][-1] == {"role": "user", "content": "describe AR"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "AR rose."}}]})

    assert azure(handler).complete("describe AR") == "AR rose."


def test_azure_error_message_never_contains_the_key():
    provider = azure(lambda request: httpx.Response(401, text="bad key sk-test-secret-value"))
    with pytest.raises(LLMError) as err:
        provider.complete("x")
    assert "401" in str(err.value)
    assert "sk-test-secret-value" not in str(err.value)


# --- the network guard ------------------------------------------------------------


def test_real_network_calls_are_blocked_in_tests():
    provider = OllamaProvider(
        base_url="http://localhost:11434",
        chat_model="m",
        embed_model="nomic-embed-text",
        embedding_dim=DIM,
    )  # default client, real transport
    with pytest.raises(RuntimeError, match="real network call"):
        provider.embed(["x"])
