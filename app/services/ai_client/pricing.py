"""Hand-maintained pricing table for AI-provider models.

Kept as a static file (not fetched at runtime) so cost accounting is
reproducible across deploys. Update this when a new model is used.

Prices are in **US-cent per 1M tokens**, split by direction:
    - "input"  → prompt tokens
    - "output" → completion tokens
    - "flat"   → charged per request regardless of tokens (rare)

Missing entries return 0 cents rather than raising — a missing price
never breaks a request, just under-reports cost. If you add a new model,
add it here at the same time.
"""
from decimal import Decimal
from typing import Optional


# (provider, model, direction) → cents per 1M tokens
PRICING_CENTS_PER_1M: dict[tuple[str, str, str], int] = {
    # OpenAI chat models
    ("openai", "gpt-4o-mini", "input"): 15,
    ("openai", "gpt-4o-mini", "output"): 60,
    ("openai", "gpt-4o", "input"): 500,
    ("openai", "gpt-4o", "output"): 1500,
    # OpenAI moderation models — the moderation endpoint itself is free.
    ("openai", "omni-moderation-latest", "input"): 0,
    ("openai", "text-moderation-latest", "input"): 0,
}


def compute_cost_cents(
    provider: str,
    model: str,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
) -> Decimal:
    """Return the total cost of one call in cents as a Decimal.

    Cost is kept fractional (sub-cent precision) so small gpt-4o-mini
    calls don't round to zero. Missing pricing entries contribute 0
    rather than raising. The DB stores this as DECIMAL(16, 8).
    """
    input_price = PRICING_CENTS_PER_1M.get((provider, model, "input"), 0)
    output_price = PRICING_CENTS_PER_1M.get((provider, model, "output"), 0)
    total_cents = Decimal(0)
    if prompt_tokens and input_price:
        total_cents += Decimal(prompt_tokens) * Decimal(input_price) / Decimal(1_000_000)
    if completion_tokens and output_price:
        total_cents += Decimal(completion_tokens) * Decimal(output_price) / Decimal(1_000_000)
    # Quantize to 8 dp to match the DB column precision.
    return total_cents.quantize(Decimal("0.00000001"))
