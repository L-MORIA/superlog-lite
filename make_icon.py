"""
Superlog-lite icon: heartbeat pulse on dark background.
Distinct from ik_qwen_icon (which is a Qwen logo).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 256


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (15, 20, 35, 255))
    draw = ImageDraw.Draw(img)

    # Background circle (dark navy)
    cx, cy = SIZE // 2, SIZE // 2
    r = 110
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(25, 35, 60, 255))

    # Outer ring (cyan)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(80, 200, 255, 255), width=6)

    # Inner ring (teal)
    r_inner = 90
    draw.ellipse(
        [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
        outline=(0, 180, 160, 255),
        width=3,
    )

    # Heartbeat line (EKG style)
    # Points: flat -> spike up -> spike down -> flat
    line_y = cy
    pts = [
        (cx - 80, line_y),
        (cx - 40, line_y),
        (cx - 30, line_y - 5),  # small bump
        (cx - 25, line_y + 5),
        (cx - 20, line_y),
        (cx - 10, line_y),
        (cx - 5, line_y - 45),  # big spike up
        (cx, line_y + 40),  # big spike down
        (cx + 5, line_y),
        (cx + 15, line_y),
        (cx + 20, line_y - 8),  # small bump
        (cx + 25, line_y + 8),
        (cx + 30, line_y),
        (cx + 80, line_y),
    ]
    draw.line(pts, fill=(255, 100, 120, 255), width=8)

    # Pulse dots at ends of heartbeat
    draw.ellipse([cx - 84, cy - 6, cx - 76, cy + 6], fill=(255, 100, 120, 255))
    draw.ellipse([cx + 76, cy - 6, cx + 84, cy + 6], fill=(255, 100, 120, 255))

    # "S" letter (Superlog) at bottom
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except (OSError, AttributeError):
        font = ImageFont.load_default()
    draw.text((cx - 10, cy + 50), "S", fill=(80, 200, 255, 255), font=font)

    # Save PNG
    out_png = Path(__file__).parent / "superlog_lite_icon.png"
    img.save(out_png)

    # Save ICO (multi-size)
    sizes = [16, 32, 48, 64, 128, 256]
    imgs = [img.resize((s, s), Image.LANCZOS) for s in sizes]
    out_ico = Path(__file__).parent / "superlog_lite_icon.ico"
    imgs[-1].save(out_ico, format="ICO", sizes=[(s, s) for s in sizes])

    print(f"PNG: {out_png}")
    print(f"ICO: {out_ico}")


if __name__ == "__main__":
    main()
