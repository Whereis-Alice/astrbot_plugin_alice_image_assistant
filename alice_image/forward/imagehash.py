"""图像感知哈希与近似重复过滤（纯函数，不依赖网络与 provider）。

近似重复去重能直接提升找图精准度：同一张图的不同压缩率 / 水印 / 尺寸版本
如果都进入候选池，会白白挤占视觉模型每轮可评估的名额，让真正不同的候选没机会被看到。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

# 哈希边长：8 表示 64 bit 指纹，是精度与成本的常见平衡点。
DEFAULT_HASH_SIZE = 8
# 汉明距离上限：64 bit 指纹最多只允许比较到 16，超过则失去「近似」含义。
MAX_HAMMING_DISTANCE = 16


@dataclass(slots=True)
class ImageFingerprint:
    """一张图片的双哈希指纹。

    average_hash 用于精确命中（快路径），difference_hash 用于汉明距离近似判定。
    """

    average_hash: str = ""
    difference_hash: str = ""
    width: int = 0
    height: int = 0

    @property
    def valid(self) -> bool:
        """两个哈希都算出来才算有效指纹。"""
        return bool(self.average_hash and self.difference_hash)


def _bits_to_hex(bits: str) -> str:
    """把 0/1 位串转成定长十六进制串，长度按位数对齐以便逐位比较。"""
    if not bits:
        return ""
    width = (len(bits) + 3) // 4
    return format(int(bits, 2), "x").zfill(width)


def _grayscale(image: Image.Image, size: tuple[int, int]) -> list[int]:
    """统一降到灰度小图，去掉颜色与分辨率差异带来的噪声。"""
    resized = image.convert("L").resize(size, Image.Resampling.LANCZOS)
    # 用 tobytes 取像素：等价于 getdata 且不依赖 Pillow 已弃用的 API。
    return list(resized.tobytes())


def average_hash_from_image(image: Image.Image, size: int = DEFAULT_HASH_SIZE) -> str:
    """aHash：与均值比较得到位串，对整体亮度分布敏感。"""
    pixels = _grayscale(image, (size, size))
    if not pixels:
        return ""
    average = sum(pixels) / len(pixels)
    bits = "".join("1" if pixel > average else "0" for pixel in pixels)
    return _bits_to_hex(bits)


def dhash_from_image(image: Image.Image, size: int = DEFAULT_HASH_SIZE) -> str:
    """dHash：比较水平相邻像素的梯度方向，对再压缩 / 加水印比 aHash 稳定得多。"""
    pixels = _grayscale(image, (size + 1, size))
    if not pixels:
        return ""
    bits: list[str] = []
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits.append("1" if left > right else "0")
    return _bits_to_hex("".join(bits))


def average_hash(image_bytes: bytes, size: int = DEFAULT_HASH_SIZE) -> str:
    """从原始字节计算 aHash，无法解码时返回空串。"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return average_hash_from_image(image, size)
    except Exception:
        return ""


def dhash(image_bytes: bytes, size: int = DEFAULT_HASH_SIZE) -> str:
    """从原始字节计算 dHash，无法解码时返回空串。"""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return dhash_from_image(image, size)
    except Exception:
        return ""


def hamming_distance(left: str, right: str) -> int:
    """两个十六进制指纹的汉明距离；长度不同或为空时返回足够大的值表示「不相似」。"""
    if not left or not right or len(left) != len(right):
        return MAX_HAMMING_DISTANCE * 4
    try:
        return int(int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return MAX_HAMMING_DISTANCE * 4


def fingerprint(
    image_bytes: bytes,
    *,
    min_resolution: int = 0,
    size: int = DEFAULT_HASH_SIZE,
) -> ImageFingerprint:
    """一次解码同时完成分辨率校验与双哈希计算，避免重复解码开销。

    分辨率不达标或无法解码时返回空指纹（valid 为 False）。
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.width, image.height
            if min_resolution > 0 and (
                width < min_resolution or height < min_resolution
            ):
                return ImageFingerprint(width=width, height=height)
            return ImageFingerprint(
                average_hash=average_hash_from_image(image, size),
                difference_hash=dhash_from_image(image, size),
                width=width,
                height=height,
            )
    except Exception:
        return ImageFingerprint()


def normalize_threshold(value: object, default: int = 5) -> int:
    """把配置里的汉明距离阈值收敛到 0..MAX_HAMMING_DISTANCE。"""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(MAX_HAMMING_DISTANCE, parsed))


class DuplicateFilter:
    """候选图去重器：aHash 精确命中走快路径，dHash + 汉明距离兜住近似重复。

    threshold 为 0 时退化为纯精确去重，行为与改造前一致，便于配置回滚。
    """

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = normalize_threshold(threshold)
        self._exact: set[str] = set()
        self._difference: list[str] = []

    def __len__(self) -> int:
        return len(self._difference)

    def is_duplicate(self, item: ImageFingerprint) -> bool:
        """先比 aHash 再比 dHash 汉明距离，命中任一即视为重复。"""
        if not item.valid:
            return False
        if item.average_hash in self._exact:
            return True
        if self.threshold <= 0:
            return item.difference_hash in self._difference
        return any(
            hamming_distance(item.difference_hash, known) <= self.threshold
            for known in self._difference
        )

    def add(self, item: ImageFingerprint) -> None:
        """登记一个已接受的指纹。"""
        if not item.valid:
            return
        self._exact.add(item.average_hash)
        self._difference.append(item.difference_hash)

    def add_if_new(self, item: ImageFingerprint) -> bool:
        """新指纹则登记并返回 True；判定为重复则返回 False。"""
        if not item.valid or self.is_duplicate(item):
            return False
        self.add(item)
        return True
