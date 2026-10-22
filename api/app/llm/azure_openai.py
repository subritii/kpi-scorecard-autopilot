"""Azure OpenAI provider: chat completions and embeddings via deployment URLs."""

import httpx

from app.llm.base import LLMProvider, Purpose, post_json


class AzureOpenAIProvider(LLMProvider):
    name = "azure_openai"

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        chat_deployment: str,
        embedding_deployment: str,
        embedding_dim: int,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ):
        super().__init__(embedding_dim)
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key  # sent only in a header; scrubbed from errors
        self.api_version = api_version
        self.chat_deployment = chat_deployment
        self.embedding_deployment = embedding_deployment
        self.embedding_model = f"azure_openai/{embedding_deployment}"
        self.client = client or httpx.Client(timeout=timeout)

    def _post(self, deployment: str, operation: str, payload: dict) -> dict:
        return post_json(
            self.client,
            f"{self.endpoint}/openai/deployments/{deployment}/{operation}",
            payload,
            self.name,
            headers={"api-key": self._api_key},
            params={"api-version": self.api_version},
            secrets=(self._api_key,),
        )

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        reply = self._post(
            self.chat_deployment, "chat/completions", {"messages": messages, "temperature": 0}
        )
        return reply["choices"][0]["message"]["content"]

    def _embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]:
        # OpenAI embedding models use the same embedding for documents and queries.
        reply = self._post(self.embedding_deployment, "embeddings", {"input": texts})
        # Each item carries the index of its input; don't rely on response order.
        return [item["embedding"] for item in sorted(reply["data"], key=lambda i: i["index"])]
