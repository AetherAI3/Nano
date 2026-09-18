"""Regenerate the compact terminal demo used at the top of the README."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 1000, 560
BACKGROUND = "#071018"
PANEL = "#0b1722"
BORDER = "#1f3545"
TEXT = "#d7e6ee"
MUTED = "#7795a6"
CYAN = "#22d3ee"
GREEN = "#4ade80"
YELLOW = "#facc15"
OUTPUT = Path(__file__).with_name("nano-demo.gif")


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    family = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu") / family,
        Path("/usr/local/share/fonts") / family,
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


TITLE = font(19, bold=True)
BODY = font(18)
BODY_BOLD = font(18, bold=True)
SMALL = font(14)


LINES: list[tuple[str, str]] = [
    ("$ pip install aether-nano", CYAN),
    ("✓ installed aether-nano", GREEN),
    ("", TEXT),
    ("$ nano library search momentum", CYAN),
    ("ID                                      IR       HOST SIGNALS", MUTED),
    ("momentum/absolute_momentum_filter       1.0.0    close", TEXT),
    ("momentum/rsi_oversold_reversal          0.1.0    RSI", TEXT),
    ("trend/ema_pullback_continuation         1.0.0    close", TEXT),
    ("", TEXT),
    ('$ nano intent compile "when is spy earnings" --json', CYAN),
    ('{"operation":"lookup", "effects":["data.read","llm.call"],', TEXT),
    (' "confirmation":{"mode":"submit_is_consent"}}', TEXT),
    ("", TEXT),
    ('$ nano project parse "frontend css pending review group by area"', CYAN),
    ('{"valid":true, "groupBy":"area", "sort":"updated"}', TEXT),
    ("", TEXT),
    ("same input → same plan → replayable receipt", YELLOW),
]


def render(visible: int) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, WIDTH - 20, HEIGHT - 20), 18, fill=PANEL, outline=BORDER, width=2)
    draw.ellipse((43, 43, 57, 57), fill="#fb7185")
    draw.ellipse((67, 43, 81, 57), fill="#facc15")
    draw.ellipse((91, 43, 105, 57), fill="#4ade80")
    draw.text((132, 38), "nano — deterministic rules", font=TITLE, fill=TEXT)
    draw.text((760, 42), "real CLI paths", font=SMALL, fill=MUTED)
    draw.line((42, 76, WIDTH - 42, 76), fill=BORDER, width=2)

    x, y = 48, 96
    for index, (line, color) in enumerate(LINES[:visible]):
        chosen = BODY_BOLD if line.startswith("$") or line.startswith("✓") else BODY
        draw.text((x, y), line, font=chosen, fill=color)
        y += 25

    draw.text((48, HEIGHT - 48), "Nano proposes. Your host decides.", font=SMALL, fill=MUTED)
    return image


def main() -> None:
    stops = (1, 3, 5, 9, 11, 13, 15, len(LINES))
    frames = [render(stop).convert("P", palette=Image.Palette.ADAPTIVE) for stop in stops]
    durations = [700, 850, 850, 1150, 900, 1100, 950, 2600]
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
