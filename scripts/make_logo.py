"""生成插件 logo（根目录 logo.png + assets/ 下的多尺寸与 SVG 同款位图）。

思路：4 倍超采样绘制后再降采样，得到干净的抗锯齿边缘；配色对齐 WebUI 的
默认主题 wonderland（深紫底 + 香槟金）。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录
S = 2048  # 超采样画布边长

BG_TOP = (58, 35, 82)
BG_BOTTOM = (23, 15, 38)
GOLD_LIGHT = (245, 226, 160)
GOLD_DARK = (196, 146, 40)
LENS_INK = (18, 12, 30)
CREAM = (250, 240, 210)


def linear_gradient(
    size: tuple[int, int],
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    *,
    horizontal: bool = False,
) -> Image.Image:
    width, height = size
    span = width if horizontal else height
    strip = Image.new("RGB", (span, 1) if horizontal else (1, span))
    pixels = strip.load()
    for i in range(span):
        t = i / max(1, span - 1)
        color = tuple(round(start[c] + (end[c] - start[c]) * t) for c in range(3))
        pixels[(i, 0) if horizontal else (0, i)] = color
    return strip.resize(size, Image.BILINEAR)


def mask() -> Image.Image:
    return Image.new("L", (S, S), 0)


def star_points(cx: float, cy: float, outer: float, inner: float, spikes: int = 4) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    step = math.pi / spikes
    angle = -math.pi / 2
    for index in range(spikes * 2):
        radius = outer if index % 2 == 0 else inner
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
        angle += step
    return points


def build() -> Image.Image:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 底板：圆角方形 + 竖向渐变
    plate = mask()
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, S - 1, S - 1), radius=round(S * 0.235), fill=255
    )
    canvas.paste(linear_gradient((S, S), BG_TOP, BG_BOTTOM), (0, 0), plate)

    # 左上角柔光，避免大色块发死
    glow = mask()
    ImageDraw.Draw(glow).ellipse(
        (-S * 0.25, -S * 0.35, S * 0.72, S * 0.55), fill=70
    )
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.06))
    glow = Image.composite(glow, mask(), plate)
    canvas.paste(Image.new("RGB", (S, S), (140, 110, 190)), (0, 0), glow)

    gold = linear_gradient((S, S), GOLD_LIGHT, GOLD_DARK)

    cx, cy = S * 0.435, S * 0.415
    outer = S * 0.245
    ring_width = round(S * 0.055)

    # 镜柄：先画，让镜环盖住接缝
    handle = mask()
    hd = ImageDraw.Draw(handle)
    angle = math.radians(45)
    x0 = cx + math.cos(angle) * (outer - ring_width * 0.35)
    y0 = cy + math.sin(angle) * (outer - ring_width * 0.35)
    x1, y1 = S * 0.815, S * 0.815
    hw = round(S * 0.078)
    hd.line((x0, y0, x1, y1), fill=255, width=hw)
    for px, py in ((x0, y0), (x1, y1)):
        hd.ellipse((px - hw / 2, py - hw / 2, px + hw / 2, py + hw / 2), fill=255)
    canvas.paste(gold, (0, 0), handle)

    # 镜片内的深色玻璃
    glass = mask()
    ImageDraw.Draw(glass).ellipse(
        (cx - outer + ring_width * 0.5, cy - outer + ring_width * 0.5,
         cx + outer - ring_width * 0.5, cy + outer - ring_width * 0.5),
        fill=236,
    )
    canvas.paste(Image.new("RGB", (S, S), LENS_INK), (0, 0), glass)

    # 镜片里的「画」：远山 + 月亮 + 星点，暗示这是找图工具
    art = mask()
    ad = ImageDraw.Draw(art)
    ad.polygon(
        [
            (cx - outer * 0.78, cy + outer * 0.60),
            (cx - outer * 0.20, cy - outer * 0.10),
            (cx + outer * 0.30, cy + outer * 0.60),
        ],
        fill=255,
    )
    ad.polygon(
        [
            (cx - outer * 0.10, cy + outer * 0.60),
            (cx + outer * 0.36, cy + outer * 0.05),
            (cx + outer * 0.82, cy + outer * 0.60),
        ],
        fill=180,
    )
    art = Image.composite(art, mask(), glass)
    canvas.paste(gold, (0, 0), art)

    moon_full = mask()
    ImageDraw.Draw(moon_full).ellipse(
        (cx + outer * 0.18, cy - outer * 0.72, cx + outer * 0.68, cy - outer * 0.22),
        fill=255,
    )
    moon_cut = mask()
    ImageDraw.Draw(moon_cut).ellipse(
        (cx + outer * 0.32, cy - outer * 0.78, cx + outer * 0.88, cy - outer * 0.22),
        fill=255,
    )
    moon = Image.composite(mask(), moon_full, moon_cut)
    canvas.paste(Image.new("RGB", (S, S), CREAM), (0, 0), moon)

    dust = mask()
    dd = ImageDraw.Draw(dust)
    for fx, fy, fr in ((-0.55, -0.45, 0.030), (-0.18, -0.62, 0.020), (0.02, -0.30, 0.014)):
        dd.ellipse(
            (cx + outer * fx - outer * fr, cy + outer * fy - outer * fr,
             cx + outer * fx + outer * fr, cy + outer * fy + outer * fr),
            fill=210,
        )
    dust = Image.composite(dust, mask(), glass)
    canvas.paste(Image.new("RGB", (S, S), CREAM), (0, 0), dust)

    # 高光：镜片左上的一道弧
    shine = mask()
    ImageDraw.Draw(shine).ellipse(
        (cx - outer * 0.72, cy - outer * 0.74, cx - outer * 0.02, cy - outer * 0.16),
        fill=44,
    )
    shine = Image.composite(shine, mask(), glass)
    canvas.paste(Image.new("RGB", (S, S), (255, 255, 255)), (0, 0), shine)

    # 镜环
    ring = mask()
    ImageDraw.Draw(ring).ellipse(
        (cx - outer, cy - outer, cx + outer, cy + outer),
        outline=255,
        width=ring_width,
    )
    canvas.paste(gold, (0, 0), ring)

    # 四芒星点缀：把"魔法感"补上
    sparkle = mask()
    sd = ImageDraw.Draw(sparkle)
    for fx, fy, size_factor, alpha in (
        (0.795, 0.215, 0.082, 255),
        (0.170, 0.775, 0.052, 225),
        (0.660, 0.088, 0.034, 200),
    ):
        sd.polygon(
            star_points(S * fx, S * fy, S * size_factor, S * size_factor * 0.26),
            fill=alpha,
        )
    halo = sparkle.filter(ImageFilter.GaussianBlur(S * 0.012)).point(lambda v: v // 3)
    halo = Image.composite(halo, mask(), plate)
    canvas.paste(Image.new("RGB", (S, S), CREAM), (0, 0), halo)
    sparkle = Image.composite(sparkle, mask(), plate)
    canvas.paste(Image.new("RGB", (S, S), CREAM), (0, 0), sparkle)

    # 内描边，贴近现代 app 图标的收边手法
    edge = mask()
    ImageDraw.Draw(edge).rounded_rectangle(
        (round(S * 0.012), round(S * 0.012), S - 1 - round(S * 0.012), S - 1 - round(S * 0.012)),
        radius=round(S * 0.225),
        outline=52,
        width=round(S * 0.010),
    )
    canvas.paste(Image.new("RGB", (S, S), GOLD_LIGHT), (0, 0), edge)
    return canvas


def main() -> None:
    art = build()
    assets = ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for target, size in (
        (ROOT / "logo.png", 512),
        (assets / "logo.png", 512),
        (assets / "logo-256.png", 256),
        (assets / "logo-128.png", 128),
        (assets / "logo-64.png", 64),
    ):
        art.resize((size, size), Image.LANCZOS).save(target, format="PNG", optimize=True)
        print(f"wrote {target.name} ({size}px)")


if __name__ == "__main__":
    main()
