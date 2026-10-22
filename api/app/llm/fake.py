"""Fake provider for tests and offline runs. Deterministic; never touches the network."""

import hashlib
import math
import re

from app.llm.base import LLMProvider, Purpose

STOPWORDS = frozenset(
    "a an and are as at be but by for from in is it its of on or that the this to was were "
    "with".split()
)


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(
        self,
        embedding_dim: int,
        fixed: dict[str, list[float]] | None = None,
        completion: str = "(fake completion)",
    ):
        super().__init__(embedding_dim)
        self.fixed = fixed or {}  # exact text -> vector, for tests that need precise control
        self.completion = completion
        self.calls: list[dict] = []  # every complete() call, for assertions

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self.completion

    def _embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]:
        return [self.fixed[t] if t in self.fixed else self._hashed(t) for t in texts]

    def _hashed(self, text: str) -> list[float]:
        """Bag of words hashed into embedding_dim buckets, scaled to length 1.

        Texts that share words point in similar directions, so similarity
        search behaves sensibly in tests. md5 (not Python's hash(), which
        changes per process) keeps vectors identical across runs.
        """
        vector = [0.0] * self.embedding_dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if word not in STOPWORDS:
                digest = hashlib.md5(word.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.embedding_dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector
