"""VLM ????????????????????????"""

from __future__ import annotations

import asyncio
import base64
import json
import re

from astrbot.api import logger
from astrbot.api.provider import Provider

# ??????????????? >10 ???????? 1 ??
SELECTION_RATIO = 0.12


class VlmReviewError(RuntimeError):
    """The visual-review service failed or returned an invalid response."""


def _normalize_indices(
    raw: list[int], total_images_count: int, max_selection: int
) -> list[int]:
    """?????????????????????? max_selection ??

    VLM ????? prompt ?????????????????"????"? max_selection
    ???????????"???"???????????
    """
    valid = [n for n in raw if 1 <= n <= total_images_count]
    return list(dict.fromkeys(valid))[:max_selection]


def _validated_indices(
    raw: list[object], total_images_count: int, max_selection: int
) -> list[int]:
    if not raw:
        return []
    parsed = [
        int(value)
        for value in raw
        if isinstance(value, (int, str)) and str(value).isdigit()
    ]
    normalized = _normalize_indices(parsed, total_images_count, max_selection)
    if not normalized:
        raise ValueError("selected_indices ??????????????")
    return normalized


async def select_from_collage(
    image_bytes: bytes,
    prompt: str,
    total_images_count: int,
    vlm_provider: Provider,
    max_selection: int | None = None,
) -> list[int]:
    """? VLM ??????????????? 1-based ???????? 1..total_images_count??

    ``max_selection`` ???????????????????????????
    SELECTION_RATIO ?????"????"???????
    """
    base64_str = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"base64://{base64_str}"

    if max_selection is None:
        max_selection = (
            1
            if total_images_count <= 10
            else max(1, round(total_images_count * SELECTION_RATIO))
        )
    else:
        max_selection = max(1, min(max_selection, total_images_count))

    prompt_template = (
        "This is a grid of images, each with a numeric label. Please observe each "
        "labeled image carefully.\n"
        f"Based on the following description: '{prompt}', identify all matching images.\n\n"
        f"You must select a maximum of {max_selection} image(s).\n\n"
        "If none of the images match the requested subject, medium, or key attributes, "
        "return an empty list. Never select an unrelated image merely to provide a result.\n\n"
        'Your response MUST be a JSON object containing a single key "selected_indices".\n'
        "The value should be a list of the numeric labels of all matching images.\n"
        "Do not include any other text, explanations, or markdown formatting "
        "outside of the JSON object.\n\n"
        "Example of a valid response:\n"
        '{"selected_indices": [1, 5, 8]}'
    )

    retries = 3
    for attempt in range(retries):
        try:
            response = await vlm_provider.text_chat(
                prompt=prompt_template, image_urls=[image_url]
            )
            result = getattr(response, "completion_text", "") or ""
            if not result and getattr(response, "result_chain", None) is not None:
                result = response.result_chain.get_plain_text()
            result = str(result or "").strip()
            logger.debug(f"[alice_image_serpapi] VLM ????: '{result}'")

            try:
                json_match = re.search(r"\{.*\}", result, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group(0))
                    selected = data.get("selected_indices")
                    if isinstance(selected, list):
                        return _validated_indices(
                            selected, total_images_count, max_selection
                        )
            except (json.JSONDecodeError, AttributeError, TypeError):
                logger.debug("[alice_image_serpapi] VLM JSON ???????????")

            fallback = re.search(
                r'["\']?selected_indices["\']?\s*:\s*\[([^\]]*)\]',
                result,
                re.IGNORECASE | re.DOTALL,
            )
            if fallback:
                content = fallback.group(1).strip()
                if not content:
                    return []
                numbers = [int(n) for n in re.findall(r"-?\d+", content)]
                if not numbers:
                    raise ValueError("selected_indices ???????????????")
                return _validated_indices(
                    numbers, total_images_count, max_selection
                )

            raise ValueError("VLM ????????? selected_indices ???")

        except Exception as e:  # noqa: BLE001 - ??
            logger.warning(
                f"[alice_image_serpapi] VLM ??? {attempt + 1}/{retries} ???: {e}"
            )
            if attempt < retries - 1:
                await asyncio.sleep(2)

    logger.error("[alice_image_serpapi] VLM ?????????")
    raise VlmReviewError("????????????????")
