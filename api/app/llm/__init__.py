"""Pick the LLM provider from settings: LLM_PROVIDER = ollama (default) | azure_openai | fake."""

import os
from collections.abc import Mapping

from app.llm.azure_openai import AzureOpenAIProvider
from app.llm.base import LLMConfigError, LLMProvider
from app.llm.fake import FakeProvider
from app.llm.ollama import OllamaProvider

PROVIDERS = ("azure_openai", "fake", "ollama")

AZURE_SETTINGS = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
)


def _embedding_dim(env: Mapping[str, str]) -> int:
    raw = env.get("EMBEDDING_DIM", "")
    if not raw.isdigit() or int(raw) == 0:
        raise LLMConfigError(
            "EMBEDDING_DIM must be a positive whole number: the embedding model's output size "
            f"(e.g. 768 for nomic-embed-text). Got {raw!r}."
        )
    return int(raw)


def get_provider(env: Mapping[str, str] | None = None) -> LLMProvider:
    """Build the configured provider. `env` defaults to os.environ (tests pass a dict)."""
    env = os.environ if env is None else env
    name = env.get("LLM_PROVIDER") or "ollama"
    if name not in PROVIDERS:
        raise LLMConfigError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}; got {name!r}")
    dim = _embedding_dim(env)

    if name == "ollama":
        return OllamaProvider(
            base_url=env.get("OLLAMA_BASE_URL") or "http://localhost:11434",
            chat_model=env.get("OLLAMA_CHAT_MODEL") or "llama3.2:3b",
            embed_model=env.get("OLLAMA_EMBED_MODEL") or "nomic-embed-text",
            embedding_dim=dim,
        )
    if name == "azure_openai":
        # Name the missing settings (never their values, which include the key).
        missing = [k for k in AZURE_SETTINGS if not env.get(k)]
        if missing:
            raise LLMConfigError(
                f"LLM_PROVIDER=azure_openai but these settings are empty: {', '.join(missing)}"
            )
        return AzureOpenAIProvider(
            endpoint=env["AZURE_OPENAI_ENDPOINT"],
            api_key=env["AZURE_OPENAI_API_KEY"],
            api_version=env["AZURE_OPENAI_API_VERSION"],
            chat_deployment=env["AZURE_OPENAI_CHAT_DEPLOYMENT"],
            embedding_deployment=env["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"],
            embedding_dim=dim,
        )
    return FakeProvider(embedding_dim=dim)
