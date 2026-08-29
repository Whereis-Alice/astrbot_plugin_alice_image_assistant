"""终选单图全分辨率复核（A 项核心）。

拼图阶段每张候选只有 300x300 的缩略图，衣着细节、发色、配饰、文字招牌全都糊掉，
视觉模型只能靠"整体氛围像不像"来选，这是选错率最高的一环。
在拼图选出候选后，把这一张候选按接近原始分辨率单独送一次模型做结构化判定，
才能真正校验"描述里的关键要素是否逐条出现在图里"。
"""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass

from PIL import Image

from .vlm_json import coerce_bool, coerce_confidence, coerce_reason, parse_json_payload

# 单图复核的最长边上限：1280 已足够看清细节，又不会让上传体积和推理成本失控。
FINAL_VERIFY_MAX_EDGE = 1280
JPEG_QUALITY = 90
# markdown 围栏字符串，用转义写法避免与源码引号冲突。
_FENCE = "\u0060\u0060\u0060"


@dataclass(slots=True)
class FinalVerdict:
    """单图复核结论。

    error 非空表示复核链路本身出问题（provider 报错 / 解析失败），
    调用方必须按 review_fail_open 语义处理，而不是当成「不匹配」把可用图丢掉。
    """

    match: bool = False
    confidence: float | None = None
    reason: str = ""
    error: str = ""

    @property
    def errored(self) -> bool:
        """复核链路是否发生技术错误。"""
        return bool(self.error)

    def accepted(self, threshold: float) -> bool:
        """在给定置信度阈值下是否算通过；缺失 confidence 时只看 match。"""
        if self.errored or not self.match:
            return False
        if self.confidence is None:
            return True
        return self.confidence >= threshold


def build_final_verify_prompt(description: str) -> str:
    """构造中文单图复核 prompt。

    要点全部围绕「降低误判」：只认看得见的证据、禁止用画质/风格代替匹配度、
    不确定就给低分，从而让置信度阈值过滤真正起作用。
    """
    safe_desc = str(description or "")[:300].replace(_FENCE, "")
    return (
        "你是一名严格的图像审核员。下面给你一张图片，以及用户想要的图片描述。\n"
        "请逐条核对描述中的关键要素（主体、人数、性别、发色发型、服饰、动作、"
        "场景、光线、画面类型是照片还是插画），判断这张图片是否真的满足描述。\n\n"
        "用户描述：「" + safe_desc + "」\n\n"
        "【判断准则】\n"
        "1. 只依据你在图中真实看得见的事物作判断，不要推测画面之外的内容，"
        "也不要因为图片好看、清晰、构图优秀就判为匹配。\n"
        "2. 描述中的任一关键要素与图片明显冲突（例如要求真人照片却是插画、"
        "要求银发却是黑发、要求室外却是室内），一律判为不匹配。\n"
        "3. 描述未提到的细节不算冲突，不要因此扣分。\n"
        "4. 如果你看不清关键细节或无法确定，请把 confidence 给到 0.3~0.5 的低区间，"
        "不要为了给出结论而虚报高置信度。\n"
        "5. confidence 是 0 到 1 之间的小数，表示你对自己判断的把握程度。\n"
        "6. reason 用一句中文说明你看到了什么、以及为什么匹配或不匹配。\n\n"
        "【输出格式】只输出一个 JSON 对象，不要 markdown 代码块，不要额外解释：\n"
        '{"match": true, "confidence": 0.86, "reason": "银发、白色连衣裙、室外草地，均符合"}'
    )


def limit_image_edge_sync(
    image_bytes: bytes, max_edge: int = FINAL_VERIFY_MAX_EDGE
) -> bytes:
    """把图片最长边限制到 max_edge（同步，供线程池调用）。

    只在超限时重新编码；任何解码失败都原样返回，绝不因为缩图失败丢掉候选。
    """
    if max_edge <= 0:
        return image_bytes
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            longest = max(image.width, image.height)
            if longest <= max_edge:
                return image_bytes
            ratio = max_edge / float(longest)
            target = (
                max(1, int(image.width * ratio)),
                max(1, int(image.height * ratio)),
            )
            resized = image.convert("RGB").resize(target, Image.Resampling.LANCZOS)
            with io.BytesIO() as buffer:
                resized.save(buffer, format="JPEG", quality=JPEG_QUALITY)
                return buffer.getvalue()
    except Exception:
        return image_bytes


def parse_verdict(text: str) -> FinalVerdict | None:
    """解析模型返回的复核 JSON；无法解析时返回 None 交由调用方重试。"""
    data = parse_json_payload(text)
    if data is None:
        return None
    confidence = coerce_confidence(data.get("confidence"))
    match = coerce_bool(data.get("match"))
    if match is None:
        match = coerce_bool(data.get("matched"))
    if match is None:
        # 只给了 confidence 没给 match 时视为「已给出判断」，交由阈值决定去留。
        if confidence is None:
            return None
        match = True
    return FinalVerdict(
        match=bool(match),
        confidence=confidence,
        reason=coerce_reason(data.get("reason")),
    )


async def verify_candidate(
    provider: object,
    image_bytes: bytes,
    description: str,
    *,
    max_edge: int = FINAL_VERIFY_MAX_EDGE,
    retries: int = 2,
    log_prefix: str = "AliceImageForward",
) -> FinalVerdict:
    """对单张候选图做全分辨率复核，永不抛异常。

    任何失败都以 FinalVerdict(error=...) 返回，让上层按 review_fail_open 决定放行与否。
    """
    from astrbot.api import logger

    if provider is None:
        return FinalVerdict(error="终选复核没有可用的视觉模型。")
    if not image_bytes:
        return FinalVerdict(error="终选复核收到空图像数据。")

    try:
        shrunk = await asyncio.to_thread(limit_image_edge_sync, image_bytes, max_edge)
    except Exception as exc:
        logger.warning("[%s] 终选复核缩图失败，改用原图：%s", log_prefix, exc)
        shrunk = image_bytes

    image_url = "base64://" + base64.b64encode(shrunk).decode("utf-8")
    prompt = build_final_verify_prompt(description)
    attempts = max(1, int(retries) + 1)
    last_error = ""

    for attempt in range(attempts):
        try:
            response = await provider.text_chat(prompt=prompt, image_urls=[image_url])
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text and getattr(response, "result_chain", None) is not None:
                text = str(response.result_chain.get_plain_text() or "").strip()
            if not text:
                raise ValueError("终选复核模型返回空响应。")
            verdict = parse_verdict(text)
            if verdict is None:
                raise ValueError("终选复核响应未包含可解析的 JSON 判定。")
            return verdict
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            lowered = last_error.lower()
            if any(
                key in lowered
                for key in ("api key", "unauthorized", "blocked", "safety", "quota")
            ):
                logger.warning(
                    "[%s] 终选复核被服务方拒绝，终止重试：%s", log_prefix, exc
                )
                break
            logger.warning(
                "[%s] 终选复核第 %s/%s 次失败：%s",
                log_prefix,
                attempt + 1,
                attempts,
                exc,
            )
            if attempt < attempts - 1:
                await asyncio.sleep(min(2**attempt, 8))

    return FinalVerdict(error=last_error or "终选复核调用失败。")
