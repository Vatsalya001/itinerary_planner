"""Per-model token pricing from models.yaml (USD per million tokens)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    input: float = 0.0
    output: float = 0.0
    cache_read: float | None = None  # None -> billed as input
    cache_write: float | None = None  # None -> billed as input

    @classmethod
    def from_dict(cls, raw: dict[str, float] | None) -> Pricing:
        raw = raw or {}
        return cls(
            input=float(raw.get("input", 0.0)),
            output=float(raw.get("output", 0.0)),
            cache_read=float(raw["cache_read"]) if raw.get("cache_read") is not None else None,
            cache_write=float(raw["cache_write"]) if raw.get("cache_write") is not None else None,
        )

    def cost_usd(self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
                 cache_write_tokens: int = 0) -> float:
        """Cost of one call. ``input_tokens`` is the total prompt size including cached and cache-written
        tokens (LangChain's ``usage_metadata`` convention); those parts are re-priced at their own rates."""
        cache_read_tokens = max(0, min(cache_read_tokens, input_tokens))
        cache_write_tokens = max(0, min(cache_write_tokens, input_tokens - cache_read_tokens))
        plain = input_tokens - cache_read_tokens - cache_write_tokens
        read_rate = self.input if self.cache_read is None else self.cache_read
        write_rate = self.input if self.cache_write is None else self.cache_write
        total = (plain * self.input + cache_read_tokens * read_rate + cache_write_tokens * write_rate
                 + output_tokens * self.output)
        return total / 1_000_000


FREE = Pricing()


def pricing_for(model_id: str | None) -> Pricing:
    """Pricing for a registry model ID; unknown or pending IDs cost 0 (and are reported as such)."""
    if not model_id:
        return FREE
    from app.llm.registry import find_spec

    spec = find_spec(model_id)
    return spec.pricing if spec is not None else FREE
