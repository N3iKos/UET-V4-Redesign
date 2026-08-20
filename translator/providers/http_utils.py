"""
providers/http_utils.py — Best-effort HTTP header / error-body parsing.

Ported from v3.6's providers.py (`parse_retry_delay`, `_parse_header_int`).
These header names are conventional across OpenAI-compatible APIs but are
NOT guaranteed for an arbitrary user-added provider, so everything here is
best-effort: it never raises, and always falls back to a safe default
(03_PROVIDER_SYSTEM.md §7).
"""

from __future__ import annotations

import json
import re

# Headers that indicate a short, useful cooldown. Daily-reset headers are
# deliberately excluded: they report time until tomorrow (86400s+), which is
# useless as an inline retry delay.
_RESET_HEADERS = (
    "x-ratelimit-reset-tokens-minute",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset-requests",
)

# A header that unambiguously counts requests left *for the day*. Only this one
# proves daily exhaustion; `x-ratelimit-remaining-requests` (no -day suffix) is
# usually the current window (often per-minute) and reads 0 on an ordinary
# short rate limit — treating that as daily exhaustion is the false positive
# that made a healthy connection look permanently dead.
_REMAINING_REQUESTS_DAY_HEADERS = (
    "x-ratelimit-remaining-requests-day",
)

# The ambiguous window counter, kept only for the short-cooldown hint — never
# used to conclude daily exhaustion.
_REMAINING_REQUESTS_WINDOW_HEADERS = (
    "x-ratelimit-remaining-requests",
)

_REMAINING_TOKENS_HEADERS = (
    "x-ratelimit-remaining-tokens-minute",
    "x-ratelimit-remaining-tokens",
)

_MAX_INLINE_RESET = 120     # cap: anything larger is probably a daily reset

# Words in an error body that genuinely name a daily / quota / session limit.
# Only an explicit statement counts — an anonymous 429 is a temporary rate
# limit, not proof the quota is spent (UET error-classification rule).
_DAILY_QUOTA_PATTERN = re.compile(
    r"requests?\s+per\s+day|tokens?\s+per\s+day|\bRPD\b|\bTPD\b"
    r"|daily\s+(?:limit|quota)|quota\s+exceeded|quota\s+exhausted"
    r"|insufficient_quota|out\s+of\s+quota|billing|exceeded\s+your\s+quota"
    r"|monthly\s+(?:limit|quota)",
    re.IGNORECASE,
)


def parse_time_str(s: str) -> int:
    """'59s' | '1m30s' | '1m30.5s' | '90' → seconds (int, min 1)."""
    s = str(s).strip()
    m = re.match(r"^(?:(\d+)m)?(?:([\d.]+)s)?$", s)
    if m and (m.group(1) or m.group(2)):
        return max(1, int(int(m.group(1) or 0) * 60 + float(m.group(2) or 0)))
    try:
        return max(1, int(float(s)))
    except ValueError:
        return 5


def parse_header_int(headers: dict, keys: tuple[str, ...] | list[str]) -> int:
    """First matching header as int, or -1 if absent/unparseable."""
    for k in keys:
        raw = headers.get(k)
        if raw is None:
            raw = headers.get(k.lower())
        if raw is None:
            continue
        val = str(raw).strip().replace(",", "")
        if not val:
            continue
        try:
            return int(float(val))
        except (ValueError, TypeError):
            continue
    return -1


def parse_retry_delay(response_text: str, headers: dict, fallback: int = 45) -> int:
    """
    Best-effort extraction of a retry delay (seconds).

    Priority: structured RetryInfo in the body > Retry-After > short reset
    headers > free-text "try again in Xs". Anything unrecognised falls back
    to `fallback` — never raises.
    """
    delay = -1

    # 1. Structured RetryInfo (Google-style error details). Bonus parsing only.
    try:
        data = json.loads(response_text)
        for detail in data.get("error", {}).get("details", []):
            if "RetryInfo" in detail.get("@type", "") and "retryDelay" in detail:
                return parse_time_str(detail["retryDelay"])
    except Exception:
        pass

    # 2. Standard Retry-After header.
    retry_after = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if retry_after:
        try:
            delay = max(delay, int(float(retry_after)))
        except ValueError:
            pass

    # 3. Short reset headers.
    for hdr in _RESET_HEADERS:
        val = str(headers.get(hdr) or headers.get(hdr.title()) or "").strip()
        if val:
            parsed = parse_time_str(val) + 1
            if parsed <= _MAX_INLINE_RESET:
                delay = max(delay, parsed)

    # 4. Free-text message fallback: "try again in 46.72s".
    try:
        msg = json.loads(response_text).get("error", {}).get("message", "")
    except Exception:
        msg = response_text or ""
    match = re.search(r"try again in ([\d.]+)\s*s", str(msg), re.IGNORECASE)
    if match:
        delay = max(delay, int(float(match.group(1))) + 1)

    return delay if delay > 0 else fallback


def classify_rate_limit(response_text: str, headers: dict) -> tuple[bool, bool]:
    """
    Classify a 429 as (is_daily_exhaustion, is_temporary).

    Daily exhaustion is only concluded on *positive* evidence:
      * a `x-ratelimit-remaining-requests-day` header that reads exactly 0, or
      * an error body that explicitly names a daily / quota / billing limit.

    Everything else — an anonymous 429, a `retry-after`, a window counter at 0
    with no day-scoped header — is a temporary rate limit. An unknown provider
    therefore degrades to "temporary cooldown", never to a false permanent
    exhaustion (03_PROVIDER_SYSTEM.md §7 / UET error-classification rule).
    """
    remaining_day = parse_header_int(headers, _REMAINING_REQUESTS_DAY_HEADERS)
    if remaining_day == 0:
        return True, False

    text = response_text or ""
    if _DAILY_QUOTA_PATTERN.search(text):
        return True, False
    return False, True


def remaining_tokens(headers: dict) -> int:
    return parse_header_int(headers, _REMAINING_TOKENS_HEADERS)


def remaining_requests(headers: dict) -> int:
    return parse_header_int(headers, _REMAINING_REQUESTS_WINDOW_HEADERS)


def is_quota_error(response_text: str) -> bool:
    """
    Does this error body explicitly name a quota / daily / billing limit?

    Used for non-429 statuses (some providers report quota exhaustion as 403
    with `insufficient_quota`), so the classifier can tell a real quota wall
    from a plain auth failure.
    """
    return bool(_DAILY_QUOTA_PATTERN.search(response_text or ""))
