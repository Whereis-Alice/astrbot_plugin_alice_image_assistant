from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit


_PIXIV_URL_PATTERN = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:(?:www|m|touch)\.)?pixiv\.net/[^\s<>\"'，。；：！？、）】》」』”’]+",
    re.IGNORECASE,
)
_PIXIV_HOSTS = {
    "pixiv.net",
    "www.pixiv.net",
    "m.pixiv.net",
    "touch.pixiv.net",
}
_LOCALE_PATTERN = re.compile(r"[a-z]{2}(?:-[a-z]{2})?", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>，。；：！？、）】》」』”’"


def extract_pixiv_artwork_id(message: str) -> str | None:
    """Return the first artwork ID found in a Pixiv artwork URL."""
    for match in _PIXIV_URL_PATTERN.finditer(message or ""):
        candidate = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        if "://" not in candidate:
            candidate = f"https://{candidate}"

        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue

        if (parsed.hostname or "").lower().rstrip(".") not in _PIXIV_HOSTS:
            continue

        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        if path_parts and _LOCALE_PATTERN.fullmatch(path_parts[0]):
            path_parts = path_parts[1:]

        if (
            len(path_parts) >= 2
            and path_parts[0].lower() == "artworks"
            and path_parts[1].isdigit()
        ):
            return path_parts[1]

        if len(path_parts) == 1 and path_parts[0].lower() == "member_illust.php":
            legacy_ids = parse_qs(parsed.query).get("illust_id", [])
            if legacy_ids and legacy_ids[0].isdigit():
                return legacy_ids[0]

    return None
