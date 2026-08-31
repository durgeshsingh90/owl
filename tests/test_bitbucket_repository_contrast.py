from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLESHEET = Path(__file__).parents[1] / "static/bitbucket_search/bitbucket_search.css"


def _declarations(css: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css)
    assert match is not None, f"Missing CSS selector: {selector}"
    return dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", match.group(1)))


def _rgba(value: str) -> tuple[float, float, float, float]:
    if value.startswith("#"):
        hexadecimal = value[1:]
        if len(hexadecimal) == 3:
            hexadecimal = "".join(character * 2 for character in hexadecimal)
        return (*[int(hexadecimal[index : index + 2], 16) / 255 for index in (0, 2, 4)], 1)
    match = re.fullmatch(r"rgb\((\d+) (\d+) (\d+) / ([\d.]+)%\)", value)
    assert match is not None, f"Unsupported CSS color: {value}"
    red, green, blue, alpha = map(float, match.groups())
    return red / 255, green / 255, blue / 255, alpha / 100


def _luminance(channels: tuple[float, ...]) -> float:
    linear = tuple(
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels[:3]
    )
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def test_repository_pill_uses_a_theme_scoped_readable_text_color():
    css = STYLESHEET.read_text(encoding="utf-8")
    default = _declarations(css, ".bitbucket-shell")
    light = _declarations(css, '.bitbucket-shell[data-theme="light"]')
    pill = _declarations(css, ".bb-repository-pill")

    assert pill["color"] == "var(--bb-repository-text)"
    assert default["--bb-repository-text"] != light["--bb-repository-text"]
    assert pill["background"] == "var(--bb-blue-soft)"
    assert pill["font-weight"] == "600"


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("surface", ("--bb-surface-soft", "--bb-surface-hover"))
def test_repository_pill_text_contrasts_with_normal_and_hovered_rows(theme, surface):
    css = STYLESHEET.read_text(encoding="utf-8")
    palette = _declarations(css, ".bitbucket-shell")
    if theme == "light":
        palette.update(_declarations(css, '.bitbucket-shell[data-theme="light"]'))

    text = _rgba(palette["--bb-repository-text"])
    tint = _rgba(palette["--bb-blue-soft"])
    row = _rgba(palette[surface])
    background = tuple(tint[index] * tint[3] + row[index] * (1 - tint[3]) for index in range(3))
    luminances = (_luminance(text), _luminance(background))
    contrast = (max(luminances) + 0.05) / (min(luminances) + 0.05)

    assert contrast >= 4.5, f"{theme} {surface} repository text contrast is {contrast:.2f}:1"
