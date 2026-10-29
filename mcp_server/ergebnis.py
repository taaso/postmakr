"""Fehler-Rückgaben der Tools.

Die Tools antworten dem Client mit Text, auch wenn etwas schiefgeht: So kann
die KI die Meldung lesen und reagieren. Damit das Aktivitätsjournal solche
Antworten trotzdem als gescheitert vermerkt, geben die Tools sie als
``Fehler`` zurück. Für den Client ist das ein ganz normaler String.
"""

from __future__ import annotations


class Fehler(str):
    """Text-Antwort eines gescheiterten Tool-Aufrufs."""


def fehler(text: str) -> Fehler:
    return Fehler(text)


def ist_fehler(ergebnis) -> bool:
    return isinstance(ergebnis, Fehler)
