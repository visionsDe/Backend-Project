"""Product-moderation helpers."""
import json
from typing import Optional

from app.services.ai_client import Feature, get_ai_client

# Strict moderation prompt — catches subtle/implied content that the moderation endpoint misses.
_GPT4_MODERATION_SYSTEM_PROMPT = (
    "You are a strict content moderator for an online marketplace where people sell "
    "products and services.\n\n"
    "LANGUAGE HANDLING: listings may be written in ANY language and the language "
    "itself is NEVER a reason to flag. Before applying any of the rules below, "
    "mentally translate the listing text into English so foreign-language terms are "
    "evaluated by MEANING, not by literal spelling. Do not skip translation for "
    "languages you find unusual — flag based on the translated meaning even for "
    "Chinese, Japanese, Korean, Arabic, Turkish, Russian, Thai, Hindi, or any other "
    "language. Examples of terms that must be caught by their translated meaning: "
    "'arma' / 'pistola' / 'fucile' / 'wapen' / 'ปืน' = firearm, "
    "'faca' / 'coltello' / 'cuchillo' / 'couteau' / 'Messer' / 'нож' = knife, "
    "'droga' / 'droghe' / 'drogue' / 'наркотики' = drugs, "
    "'cocaína' / 'cocaïne' / 'кокаин' = cocaine, "
    "'maconha' / 'marijuana' / 'hierba' / 'cannabis' = cannabis, "
    "'álcool' / 'alcohol' / 'alcool' / 'Alkohol' = alcohol. "
    "Brand names, model numbers, proper nouns, and pure numbers are language-neutral "
    "and do not require translation.\n\n"
    "Your job is to flag ANY listing that could be inappropriate — in any language — "
    "including content with plausible deniability. Flag if you detect (evaluated on "
    "the English-translated meaning):\n"
    "- Any suggestive, implied, coded, or innuendo-based references to minors or "
    "sexual content involving minors (examples: 'delicious young girls', 'cute little "
    "ones', 'fresh teens')\n"
    "- Visible weapons, firearms, knives, ammunition as product subjects\n"
    "- Drugs, drug paraphernalia, cannabis, tobacco, vaping, e-cigarettes\n"
    "- Alcohol when it is clearly the product being sold\n"
    "- Explicit or suggestive sexual content\n"
    "- Violence, gore, hate speech, or graphic imagery\n"
    "- Illegal activities or items\n"
    "- Misleading medical claims\n"
    "Be strict. If in doubt, flag it. Analyze the text and each image INDEPENDENTLY. "
    "Do NOT create a category for language, script, or unfamiliarity — those are "
    "never flag reasons on their own.\n\n"
    "Return ONLY JSON with this exact shape:\n"
    "{\n"
    '  "text_check": {"flagged": bool, "categories": [list of short category labels], "reason": "<=200 chars explanation"},\n'
    '  "image_checks": [{"index": <int>, "flagged": bool, "categories": [...], "reason": "..."}, ...]\n'
    "}\n"
    "Each image_checks entry must match the zero-based index of the image provided."
)


def check_moderation(
    text: str,
    *,
    user_id: Optional[int] = None,
    context: Optional[dict] = None,
) -> dict:
    """Check text against OpenAI's moderation API."""
    response = get_ai_client().moderate_text(
        text, feature=Feature.MODERATION, user_id=user_id, context=context,
    )
    return _parse_moderation_result(response)


def check_image_moderation(
    image_bytes: bytes,
    content_type: str,
    *,
    user_id: Optional[int] = None,
    context: Optional[dict] = None,
) -> dict:
    """Check an image against OpenAI's moderation API."""
    response = get_ai_client().moderate_image(
        image_bytes, content_type, feature=Feature.MODERATION,
        user_id=user_id, context=context,
    )
    return _parse_moderation_result(response)


def check_with_gpt4(
    text: str,
    image_bytes_list=None,
    *,
    user_id: Optional[int] = None,
    context: Optional[dict] = None,
) -> dict:
    """Second-layer moderation using GPT-4o-mini with vision.

    Catches subtle/implied content that the omni-moderation endpoint misses
    (e.g. innuendo about minors, visible weapons, drugs).

    Returns:
    {
        "text": {"flagged": bool, "categories": [str], "reason": str},
        "images": [{"index": int, "flagged": bool, "categories": [str], "reason": str}, ...],
        "raw": {...}  # full parsed JSON response for audit
    }
    """
    import base64

    content = [{"type": "text", "text": f"Item name: {text}"}]
    for ct, data in image_bytes_list or []:
        b64 = base64.b64encode(data).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}})

    response = get_ai_client().chat(
        messages=[
            {"role": "system", "content": _GPT4_MODERATION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        feature=Feature.MODERATION,
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        temperature=0,
        operation="moderation_review",
        user_id=user_id,
        context=context,
    )

    raw_content = response.choices[0].message.content
    try:
        parsed = json.loads(raw_content)
    except Exception:
        parsed = {}

    text_check = parsed.get("text_check") or {}
    image_checks = parsed.get("image_checks") or []

    return {
        "text": {
            "flagged": bool(text_check.get("flagged")),
            "categories": text_check.get("categories") or [],
            "reason": text_check.get("reason") or "",
        },
        "images": [
            {
                "index": int(c.get("index", i)),
                "flagged": bool(c.get("flagged")),
                "categories": c.get("categories") or [],
                "reason": c.get("reason") or "",
            }
            for i, c in enumerate(image_checks)
        ],
        "raw": parsed,
    }


def _parse_moderation_result(response) -> dict:
    result = response.results[0]

    # Use OpenAI's own flagged decision (uses calibrated thresholds per category).
    # The GPT-4o-mini second layer in items.py catches subtle/implied content this misses.
    flagged = bool(result.flagged)

    flagged_categories = {
        category: flagged_val
        for category, flagged_val in dict(result.categories).items()
        if flagged_val
    }

    # Serialize raw response for audit log
    try:
        raw = response.model_dump()
    except AttributeError:
        raw = {"results": [result.__dict__] if hasattr(result, '__dict__') else []}

    return {
        "flagged": flagged,
        "categories": flagged_categories,
        "raw": raw,
    }
