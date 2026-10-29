"""Bedeutungs-Vektoren fuer die Suche (Sounds, Materiallager).

Drei lokale Modelle, alle laufen auf der CPU und werden erst beim ersten
Gebrauch geladen. Keine API, keine Kosten pro Suche, nichts verlaesst deinen Rechner.

- ``text``: mehrsprachige Satz-Vektoren (Deutsch, Englisch und viele mehr) fuer
  Beschreibungen, Schlagworte und Transkripte.
- ``bild`` und ``bild_text``: Standbilder und Suchtexte im selben Raum, der
  Text-Teil versteht Deutsch und Englisch ("Gummiente" wie "rubber duck").
- ``klang`` und ``klang_text``: Sounds und Klangbeschreibungen im selben Raum.
  Das Modell hoert den Sound selbst. Der Text-Teil versteht nur Englisch.

Alle Vektoren sind auf Laenge 1 normiert, Aehnlichkeit = Skalarprodukt.
App und MCP fragen ueber ``vektor_client.py`` an.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import numpy as np

MODELLE = {
    "text": "intfloat/multilingual-e5-small",
    "bild": "google/siglip-base-patch16-256-multilingual",
    "klang": "laion/clap-htsat-unfused",
}
# Festgelegte Staende der Modelle, damit jede Installation dieselben Gewichte laedt
REVISIONEN = {
    "text": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    "bild": "8952a4eafcde3cb7ab46b1dd629b33f8784ca9c6",
    "klang": "8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a",
}
KLANG_SR = 48000
KLANG_MAX_S = 10.0

_geladen: dict[str, object] = {}
_sperre = threading.Lock()


def _laden(art: str):
    with _sperre:
        if art not in _geladen:
            import torch  # noqa: PLC0415
            torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
            if art == "text":
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
                _geladen[art] = SentenceTransformer(MODELLE[art], device="cpu", revision=REVISIONEN[art])
            elif art == "bild":
                from transformers import AutoModel, AutoProcessor  # noqa: PLC0415
                rev = REVISIONEN[art]
                _geladen[art] = (AutoModel.from_pretrained(MODELLE[art], revision=rev).eval(),
                                 AutoProcessor.from_pretrained(MODELLE[art], revision=rev))
            else:
                from transformers import ClapModel, ClapProcessor  # noqa: PLC0415
                rev = REVISIONEN[art]
                _geladen[art] = (ClapModel.from_pretrained(MODELLE[art], revision=rev).eval(),
                                 ClapProcessor.from_pretrained(MODELLE[art], revision=rev))
        return _geladen[art]


def im_cache(art: str) -> bool:
    """Liegt das Modell schon auf der Platte? Fragt nur den Cache, laedt nichts."""
    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
    except ImportError:
        return False
    treffer = try_to_load_from_cache(MODELLE[art], "config.json", revision=REVISIONEN[art])
    return isinstance(treffer, str)


def _norm(m) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


def _np(aus) -> np.ndarray:
    # Neuere transformers-Versionen liefern ein Ausgabe-Objekt statt eines Tensors.
    t = getattr(aus, "pooler_output", aus)
    return t.detach().cpu().numpy()


def text(texte: list[str], frage: bool = False) -> np.ndarray:
    """Satz-Vektoren. frage=True fuer Suchanfragen, sonst fuer die durchsuchten Texte."""
    vorsatz = "query: " if frage else "passage: "
    return _norm(_laden("text").encode([vorsatz + t for t in texte], batch_size=32))


def bild(pfade: list[str | Path]) -> np.ndarray:
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    modell, proz = _laden("bild")
    aus = []
    for i in range(0, len(pfade), 16):
        bilder = [Image.open(p).convert("RGB") for p in pfade[i:i + 16]]
        with torch.no_grad():
            aus.append(_np(modell.get_image_features(**proz(images=bilder, return_tensors="pt"))))
    return _norm(np.concatenate(aus)) if aus else np.zeros((0, 768), np.float32)


def bild_text(texte: list[str]) -> np.ndarray:
    import torch  # noqa: PLC0415
    modell, proz = _laden("bild")
    with torch.no_grad():
        return _norm(_np(modell.get_text_features(**proz(text=texte, padding="max_length", return_tensors="pt"))))


def _audio(pfad: str | Path) -> np.ndarray:
    roh = subprocess.run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-i", str(pfad), "-t", str(KLANG_MAX_S), "-ac", "1",
                          "-ar", str(KLANG_SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(roh, np.float32)
    return x if len(x) else np.zeros(KLANG_SR // 10, np.float32)


def klang(pfade: list[str | Path]) -> np.ndarray:
    import torch  # noqa: PLC0415
    modell, proz = _laden("klang")
    aus = []
    for i in range(0, len(pfade), 8):
        eingabe = proz(audio=[_audio(p) for p in pfade[i:i + 8]], sampling_rate=KLANG_SR, return_tensors="pt", padding=True)
        with torch.no_grad():
            aus.append(_np(modell.get_audio_features(**eingabe)))
    return _norm(np.concatenate(aus)) if aus else np.zeros((0, 512), np.float32)


def klang_text(texte: list[str]) -> np.ndarray:
    import torch  # noqa: PLC0415
    modell, proz = _laden("klang")
    with torch.no_grad():
        return _norm(_np(modell.get_text_features(**proz(text=texte, return_tensors="pt", padding=True))))


ARTEN = {"text": text, "bild": bild, "bild_text": bild_text, "klang": klang, "klang_text": klang_text}
