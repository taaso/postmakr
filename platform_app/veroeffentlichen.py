#!/usr/bin/env python3
"""Andockstelle fuer eigenes Veroeffentlichen.

Die lokale Fassung postet nichts selbst. Ein Beitrag wird freigegeben (er
wandert von drafts/ nach archive/), geplant und von Hand als gepostet
markiert. Wer seine Beitraege automatisch an einen Kanal schicken will,
baut die Anbindung hier ein: bei_freigabe() wird direkt nach jeder
erfolgreichen Freigabe aufgerufen, im Dashboard wie auf dem Handy.

Regeln fuer eine eigene Anbindung:
  - Die Funktion laeuft im Request. Was laenger dauert, gehoert in einen
    eigenen Thread oder Prozess.
  - Fehler werden vom Aufrufer abgefangen und ins Aktivitaets-Journal
    geschrieben. Die Freigabe selbst bleibt davon unberuehrt.
  - Zugangsdaten nie in den Workspace-Ordner schreiben, der wird gerne
    geteilt oder gesichert. Besser Umgebungsvariablen oder der
    Schluesselbund des Systems.
"""

from pathlib import Path


def bei_freigabe(ws_root: Path, beitrag_dir: Path, meta: dict) -> None:
    """Wird nach jeder Freigabe aufgerufen.

    ws_root      Wurzel des Workspace.
    beitrag_dir  Ordner des freigegebenen Beitrags unter archive/.
    meta         Inhalt der meta.yaml nach der Freigabe (thema, format, ...).

    Standard: tut nichts.
    """
    return None
