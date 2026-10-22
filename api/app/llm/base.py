"""The LLM provider interface: complete() for text, embed() for vectors.

Everything provider-specific lives in this package (rule 7); the rest of the
app only sees LLMProvider. The LLM describes, it never computes numbers
(rules 1 and 2), so nothing here does any KPI math.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Literal

import httpx

# Some embedding models are trained to treat stored documents and search
# queries differently, so callers say which one they are embedding.
Purpose = Literal["document", "query"]


class LLMError(Exception):
    """A provider call failed (unreachable, HTTP error, or a malformed reply)."""


class LLMConfigError(LLMError):
    """Settings in .env are missing or invalid."""


class EmbeddingDimensionError(LLMError):
    """The model's embedding size doesn't match EMBEDDING_DIM."""


class LLMProvider(ABC):
    name: str
    embedding_model: str  # stored with each vector in doc_chunks.embedding_model

    def __init__(self, embedding_dim: int):
        self.embedding_dim = embedding_dim

    @abstractmethod
    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """Return the model's reply to `prompt` (temperature 0)."""

    def embed(self, texts: list[str], purpose: Purpose = "document") -> list[list[float]]:
        """One vector per text, each exactly EMBEDDING_DIM long, or a clear error."""
        if not texts:
            return []
        vectors = self._embed(list(texts), purpose)
        if len(vectors) != len(texts):
            raise LLMError(f"{self.name}: sent {len(texts)} texts but got {len(vectors)} vectors")
        for vector in vectors:
            if len(vector) != self.embedding_dim:
                raise EmbeddingDimensionError(
                    f"{self.name} returned {len(vector)}-dimensional embeddings, but "
                    f"EMBEDDING_DIM={self.embedding_dim}. Set EMBEDDING_DIM to the model's "
                    "output size (doc_chunks.embedding is created with that size, so changing "
                    "it needs a fresh doc_chunks table), or use a model with that size."
                )
        return vectors

    @abstractmethod
    def _embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]:
        """Provider-specific embedding call; embed() checks the result."""


def post_json(
    client: httpx.Client,
    url: str,
    payload: dict,
    provider: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> dict:
    """POST JSON and return the JSON reply, turning failures into LLMError.

    `secrets` (e.g. an API key) are scrubbed from error messages, in case the
    server echoes them back, so they never reach logs.
    """
    try:
        response = client.post(url, json=payload, headers=headers, params=params)
    except httpx.HTTPError as e:  # connection refused, timeout, ...
        raise LLMError(f"{provider}: could not reach {url} ({type(e).__name__})") from e
    if response.is_error:
        detail = response.text[:200]
        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "[redacted]")
        raise LLMError(f"{provider}: HTTP {response.status_code} from {url}: {detail}")
    return response.json()
