"""
carousel_theme_service.py
--------------------------
AI-generated carousel colour theme from a plain-English mood description.

Usage (owner portal only — NOT called during chat rendering):
    result = generate_carousel_theme("Professional, dark navy, modern executive")
    # result: {"bg": "#1e1b4b", "title_color": "#e0e7ff",
    #           "body_color": "#c7d2fe", "nav_color": "#e0e7ff"}
    # or raises ValueError with a user-friendly message on failure.

The generated theme is saved into slides.json alongside slide content.
chat.html reads it once at page-render time — no LLM call at chat time.

Feature flag: settings.CAROUSEL_AI_THEME_ENABLED
  Set CAROUSEL_AI_THEME_ENABLED=False in env to disable the endpoint entirely.
"""

import json
import re

from app.rag.llm_client import LLMClient
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_THEME_FIELDS = ("bg", "title_color", "body_color", "nav_color")

_DEFAULTS: dict[str, str] = {
    "bg":          "#e8f5f1",
    "title_color": "#0f172a",
    "body_color":  "#334155",
    "nav_color":   "#475569",
}

_PROMPT = """\
You are a UI colour designer for professional profile pages.
Given a mood description, return a JSON colour theme for a carousel banner.

Mood: "{mood}"

Return ONLY valid JSON — no markdown, no explanation:
{{
  "bg":          "#rrggbb",
  "title_color": "#rrggbb",
  "body_color":  "#rrggbb",
  "nav_color":   "#rrggbb"
}}

Strict contrast rules — MUST follow:
1. bg is the slide background colour.
2. title_color is the heading text drawn ON TOP of bg — must have contrast ratio ≥ 4.5:1 against bg.
3. body_color is the body/subtitle text ON TOP of bg — must have contrast ratio ≥ 3:1 against bg.
4. nav_color is an icon/arrow drawn on a WHITE button — must be dark enough to read on white (contrast ≥ 3:1 against #ffffff).
5. DARK bg rule: if bg luminance is low (dark colours like navy, charcoal, deep purple, black), title_color and body_color MUST be light — e.g. white, off-white, or very light grey/tint.
6. LIGHT bg rule: if bg luminance is high (white, cream, pale tones), title_color and body_color MUST be dark — e.g. near-black or dark grey.
7. title_color should be noticeably bolder/brighter than body_color.
8. Do NOT make title_color and body_color the same as or similar to bg.

Examples of correct themes:
- Dark navy bg "#0f172a" → title "#e2e8f0", body "#94a3b8", nav "#475569"
- Charcoal bg "#1f2937" → title "#f9fafb", body "#d1d5db", nav "#6b7280"
- Pale mint bg "#f0fdf4" → title "#14532d", body "#166534", nav "#4b5563"
- Warm cream bg "#fff7ed" → title "#7c2d12", body "#9a3412", nav "#6b7280"
- White bg "#ffffff" → title "#111827", body "#374151", nav "#6b7280"

All values must be valid 6-digit CSS hex codes. Keep the aesthetic professional and clean.
"""


def _valid_hex(value: str) -> bool:
    return bool(_HEX_RE.match(value if value else ""))


# ── WCAG contrast enforcement ──────────────────────────────────────────────────

def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 relative luminance (0 = black, 1 = white)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    def _lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_ratio(hex1: str, hex2: str) -> float:
    l1, l2 = _relative_luminance(hex1), _relative_luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _enforce_contrast(theme: dict[str, str]) -> dict[str, str]:
    """
    Hard-enforce readability after LLM generation.
    - title_color / body_color must meet contrast ratios against bg.
    - nav_color must be visible on the white (#ffffff) nav button.
    Falls back to safe palette colours when the LLM got it wrong.
    """
    bg = theme["bg"]
    dark_bg = _relative_luminance(bg) <= 0.18   # roughly mid-grey and darker

    # Safe palette for dark backgrounds (light text)
    _TITLE_ON_DARK = "#f1f5f9"
    _BODY_ON_DARK  = "#94a3b8"
    # Safe palette for light backgrounds (dark text)
    _TITLE_ON_LIGHT = "#0f172a"
    _BODY_ON_LIGHT  = "#334155"

    result = dict(theme)

    for field, min_ratio, dark_fallback, light_fallback in (
        ("title_color", 4.5, _TITLE_ON_DARK, _TITLE_ON_LIGHT),
        ("body_color",  3.0, _BODY_ON_DARK,  _BODY_ON_LIGHT),
    ):
        if _contrast_ratio(bg, result[field]) < min_ratio:
            result[field] = dark_fallback if dark_bg else light_fallback
            logger.debug(
                "carousel_theme_service | contrast fix applied to %s "
                "(bg=%s was_dark=%s)", field, bg, dark_bg
            )

    # nav_color is rendered as an SVG icon on a white button
    if _contrast_ratio("#ffffff", result["nav_color"]) < 3.0:
        result["nav_color"] = "#4b5563"

    return result


# ── Parse & validate ───────────────────────────────────────────────────────────

def _parse_and_validate(raw: str) -> dict[str, str]:
    """
    Extract JSON from LLM response, validate each field is a hex colour,
    then enforce WCAG contrast so text is always readable regardless of
    what the LLM returned.
    Raises ValueError if the response cannot be parsed at all.
    """
    # Strip markdown code fences if present
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    clean = re.sub(r"\s*```$", "", clean).strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse AI response as JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("AI response was not a JSON object")

    theme: dict[str, str] = {}
    for field in _THEME_FIELDS:
        val = data.get(field, "")
        theme[field] = val if _valid_hex(val) else _DEFAULTS[field]

    return _enforce_contrast(theme)


def generate_carousel_theme(mood: str) -> dict[str, str]:
    """
    Call LLM with a mood description and return a validated theme dict.

    Returns:
        dict with keys: bg, title_color, body_color, nav_color (all hex strings)

    Raises:
        ValueError: if LLM fails or response cannot be parsed
    """
    mood = mood.strip()
    if not mood:
        raise ValueError("Mood description must not be empty")

    prompt = _PROMPT.format(mood=mood[:300])  # cap to avoid prompt injection

    llm = LLMClient()
    try:
        response = llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.7,
        )
    except Exception as e:
        logger.warning("carousel_theme_service | LLM call failed: %s", e)
        raise ValueError("AI theme generation failed — please try again") from e

    raw = response.choices[0].message.content or ""
    theme = _parse_and_validate(raw)
    logger.info("carousel_theme_service | mood=%r | theme=%s", mood[:60], theme)
    return theme
