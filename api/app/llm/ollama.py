"""Ollama provider (local models): /api/chat for completions, /api/embed for vectors."""

import httpx

from app.llm.base import LLMProvider, Purpose, post_json

# nomic-embed-text was trained with these task prefixes and retrieves
# noticeably better when they are used.
NOMIC_PREFIXES = {"document": "search_document: ", "query": "search_query: "}


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        chat_model: str,
        embed_model: str,
        embedding_dim: int,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ):
        super().__init__(embedding_dim)
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.embedding_model = f"ollama/{embed_model}"
        # Local models can be slow on first load, hence the generous timeout.
        self.client = client or httpx.Client(timeout=timeout)

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        reply = post_json(
            self.client,
            f"{self.base_url}/api/chat",
            {
                "model": self.chat_model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0},
            },
            self.name,
        )
        return reply["message"]["content"]

    def _embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]:
        prefix = NOMIC_PREFIXES[purpose] if self.embed_model.startswith("nomic-embed-text") else ""
        reply = post_json(
            self.client,
            f"{self.base_url}/api/embed",
            {"model": self.embed_model, "input": [prefix + t for t in texts]},
            self.name,
        )
        return reply["embeddings"]
