"""Mitgelieferte Schriften (assets/fonts) fuer Renderer und Dashboard-Vorschau.

Der Renderer bindet sie per file://-Adresse ein, die Vorschau im Dashboard
ueber /schriften/<datei>. So sehen Slides ohne Internet genauso aus wie mit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
# (Familie, Datei, Stil, Gewichte): variable Schriften decken eine Spanne ab.
SCHRIFTEN = (
    ("Inter", "Inter[opsz,wght].ttf", "normal", "100 900"),
    ("Inter", "Inter-Italic[opsz,wght].ttf", "italic", "100 900"),
    ("Source Sans 3", "SourceSans3[wght].ttf", "normal", "200 900"),
    ("Source Sans 3", "SourceSans3-Italic[wght].ttf", "italic", "200 900"),
    ("Anton", "Anton-Regular.ttf", "normal", "400"),
    ("Archivo Black", "ArchivoBlack-Regular.ttf", "normal", "400"),
    ("Caveat", "Caveat[wght].ttf", "normal", "400 700"),
)
DATEIEN = frozenset(datei for _, datei, _, _ in SCHRIFTEN)


def schriften_css(adresse: Callable[[Path], str]) -> str:
    """@font-face-Bloecke; ``adresse`` macht aus dem Dateipfad die URL."""
    bloecke = []
    for familie, datei, stil, gewicht in SCHRIFTEN:
        pfad = FONTS_DIR / datei
        if pfad.is_file():
            bloecke.append(
                f"@font-face{{font-family:'{familie}';src:url('{adresse(pfad)}') format('truetype');"
                f"font-style:{stil};font-weight:{gewicht};font-display:block;}}"
            )
    return "\n".join(bloecke)
