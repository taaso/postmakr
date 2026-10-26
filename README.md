# postmakr lokal

Lizenz: GNU Affero General Public License 3.0 (AGPL-3.0), siehe [LICENSE](LICENSE).
Fremde Bestandteile: [THIRD_PARTY.md](THIRD_PARTY.md).

postmakr lokal ist eine KI-Content-Werkstatt, die komplett auf deinem eigenen
Rechner läuft. Du brauchst kein Konto und keine Anmeldung, und deine Daten
bleiben bei dir.

## Was postmakr lokal kann

- **Beiträge**: Karussells, Sharepics und animierte Slides aus HTML-Vorlagen,
  gerendert als PNG und MP4. Die KI baut die Beiträge, du gibst sie frei.
- **Trial Reels**: Recorder mit Teleprompter direkt im Browser, auch auf dem
  Handy (siehe Handy-Zugang).
- **Sound-Bibliothek**: Du füllst sie mit deinen eigenen Sounds und findest
  sie über Stichworte, Bedeutung oder Klang wieder.
- **Materiallager**: Fotos und Videos ablegen und nach Bild und Bedeutung
  durchsuchen, etwa „Hund am Strand“.
- **Hörcheck**: hört ein fertiges Reel an und meldet Soundeffekte, die zu
  spät kommen, zu leise sind oder mit der Stimme kollidieren.
- **Kalender und Freigabe**: Beiträge planen und bewusst freigeben.

## Was es nicht ist

postmakr lokal postet nichts auf Kanäle, sammelt keine Statistiken und hat
keinen Login. Wenn du Beiträge automatisch veröffentlichen oder Zahlen
abholen willst, baust du dir diese Anbindung selbst.

## Voraussetzungen

- Python 3.10 oder neuer
- ffmpeg (für Videos, Recorder-Export und Hörcheck).
  Mac: `brew install ffmpeg`, Linux: `sudo apt install ffmpeg`,
  Windows: `winget install ffmpeg`
- Etwa 1 GB Platz für die Grundausstattung. Mit den Extras kommen etwa
  1 GB Pakete und beim ersten Gebrauch die Modelle dazu: etwa 3 GB für die
  Suche nach Bild und Bedeutung und etwa 1,5 GB für den Hörcheck.

## Installation

```
git clone <adresse-dieses-repos> postmakr-lokal
cd postmakr-lokal
./postmakr installieren
```

Das legt eine eigene Python-Umgebung im Ordner `.venv` an, installiert die
Grundausstattung und den Browser für das Rendern. Danach fragt es, ob du die
Extras willst (Suche nach Bild und Bedeutung, Transkripte, Hörcheck). Du
kannst das auch direkt sagen: `./postmakr installieren --extras` oder
`--ohne-extras`. Ohne Extras läuft alles andere, die Suche arbeitet dann mit
Stichworten.

Unter Windows ersetzt du `./postmakr` überall durch `python postmakr.py`.

## Start

```
./postmakr start
```

Das prüft Python, ffmpeg und den Render-Browser, legt beim ersten Mal den
Datenordner mit einem ersten Workspace an und startet das Dashboard. Öffne
dann im Browser die Adresse, die im Terminal steht:
**http://127.0.0.1:4711**. Beenden mit Strg+C.

Standardmäßig ist das Dashboard nur auf deinem Rechner erreichbar, niemand
sonst im Netz sieht es.

## Handy-Zugang

Der Recorder braucht auf dem Handy Kamera und Mikrofon. Das erlauben
Handy-Browser nur über eine sichere Verbindung (HTTPS). Dafür gibt es:

```
./postmakr start --handy
```

Dann passiert Folgendes:

1. postmakr erzeugt einmalig eine eigene kleine Zertifizierungsstelle und
   damit ein Zertifikat für die Adresse deines Rechners im WLAN. Ändert
   sich die Adresse, stellt postmakr beim nächsten Start ein neues aus. Der
   private Schlüssel bleibt im Datenordner unter `.handy/` und ist nur für
   dich lesbar. Die Zertifizierungsstelle gilt nur für Adressen im eigenen
   Netz, sie kann keine Zertifikate für fremde Webseiten ausstellen.
2. Zusätzlich zu http://127.0.0.1:4711 läuft das Dashboard unter
   **https://<adresse-im-wlan>:4712**.
3. Im Terminal erscheint ein QR-Code. Er enthält die Adresse mit einem
   zufälligen Zugangscode.

**Wichtig:** Mit `--handy` ist das Dashboard für alle in deinem WLAN
erreichbar, und es gibt keinen Login. Ohne Zugangscode bekommt niemand
etwas zu sehen, der Code ist aber der einzige Schutz. Gib ihn nicht weiter
und nutze `--handy` nicht in fremden oder öffentlichen Netzen. Mit
`./postmakr start --handy --neuer-code` würfelst du einen neuen Code, alle
bisherigen Geräte sind dann ausgesperrt.

So richtest du das Handy ein (einmalig):

- Öffne auf dem Handy `https://<adresse-im-wlan>:4712/handy/` (steht im
  Terminal). Dort lädst du das Zertifikat herunter und findest die Anleitung.
- **iPhone**: Profil herunterladen, in den Einstellungen unter „Profil
  geladen“ installieren, dann unter Einstellungen > Allgemein > Info >
  Zertifikatsvertrauenseinstellungen „postmakr lokal“ einschalten.
- **Android**: Die Datei unter Einstellungen > Sicherheit > Verschlüsselung
  und Anmeldedaten > Zertifikat installieren > CA-Zertifikat auswählen.
  Chrome vertraut ihm danach.
- Danach den QR-Code aus dem Terminal scannen. Das Handy merkt sich den
  Zugang 30 Tage lang.

Geht es auch ohne Zertifikat? Du kannst die Warnung im Browser wegklicken.
Nach dem Quelltext von Safari zählt eine HTTPS-Seite auch dann als sicher,
also sollten Kamera und Mikrofon grundsätzlich funktionieren. Auf einem
iPhone getestet ist das aber nicht, Safari fragt immer wieder nach, und es
gibt Berichte, dass Hintergrundverbindungen ohne vertrautes Zertifikat
scheitern. Der verlässliche Weg ist das Zertifikat.

## Eigene Sound-Bibliothek verbinden

postmakr lokal startet ohne Sounds. Du lädst sie entweder einzeln auf der
Seite Sounds hoch oder verbindest einen ganzen Ordner, den du schon hast:

```
./postmakr sounds verbinden ~/Pfad/zu/meinen-sounds --workspace mein-studio
```

Der Ordner braucht eine `sounds.yaml` mit Nummer, Datei, Name und gern
Thema, Stimmung und Schlagworten je Sound. postmakr kopiert nichts, sondern
verknüpft den Ordner. Neue Uploads landen ebenfalls dort. Liegen im Ordner
die Unterordner `postmakr/` (Nummern 1 bis 999) und `eigene/` (ab 1001), gilt
der erste für alle Workspaces und der zweite für den angegebenen Workspace.
In der App erscheinen alle zusammen in einer Bibliothek.

Achte darauf, dass du die Sounds nutzen darfst. Sounds aus fremden Videos
gehören meist nicht dir.

## Claude verbinden

postmakr lokal bringt einen MCP-Server mit, über den Claude deine Workspaces
lesen und Beiträge bauen kann. Starte ihn mit `./postmakr mcp`. Wie du ihn
in Claude einträgst, steht in [mcp_server/README.md](mcp_server/README.md).

## Wo deine Daten liegen

Alles liegt im Datenordner, standardmäßig `~/postmakr`. Darin liegen deine
Workspaces unter `workspaces/`. Jeder Workspace ist ein Unterordner mit
Beiträgen (`drafts/`, `archive/`), Quellen, Styleguide und `brand.yaml`.
Einen anderen Ort wählst du mit der Umgebungsvariable `POSTMAKR_DATEN`, zum
Beispiel `POSTMAKR_DATEN=~/Dokumente/postmakr ./postmakr start`.

Die Modelle für Suche und Hörcheck liegen nach dem ersten Gebrauch im Cache
unter `~/.cache/huggingface`. Löschen ist jederzeit möglich, sie werden dann
beim nächsten Mal neu geladen.

## Gut zu wissen

- Farben deiner Marke stehen in `brand.yaml` (`accent` und `farben:`). Der
  Renderer übernimmt sie in die Slide-Vorlagen, die Schriften Inter und
  Source Sans 3 liegen bei.
- Wenn du Chrome installiert hast und den Render-Browser nicht laden willst:
  `./postmakr installieren --ohne-browser` und vor dem Start
  `export POSTMAKR_BROWSER_KANAL=chrome`.
- `engine-shared/tools/sinnsuche.py` gewichtet Stichworte, Bedeutung und
  Klang. Wenn du die Gewichte für deine eigene Sound-Bibliothek messen
  willst, leg dir ein Testset aus Suchfragen und erwarteten Sounds an.

## Lizenz

postmakr lokal steht unter der GNU Affero General Public License 3.0. Du darfst
es nutzen, verändern und weitergeben. Wenn du eine veränderte Fassung
weitergibst oder anderen über ein Netzwerk zur Verfügung stellst, musst du
deren Quelltext unter derselben Lizenz offenlegen. Den vollen Text findest du
in [LICENSE](LICENSE), die Lizenzen der mitgelieferten Schriften und Modelle
in [THIRD_PARTY.md](THIRD_PARTY.md).

---

## English summary

postmakr lokal is an AI content workshop that runs entirely on your own
computer, with no account and no login: posts (carousels, sharepics,
animated slides rendered to PNG and MP4), trial reels with an in-browser
recorder and teleprompter, a sound library you fill yourself, a media
library searchable by image and meaning, a sound check for finished reels,
a calendar and an approval step. It does not post to social channels,
collect statistics or have user accounts.

Requirements: Python 3.10+, ffmpeg. Install with `./postmakr installieren`
(add `--extras` for semantic search, transcripts and the sound check; the
models, about 3 GB for search and 1.5 GB for the sound check, download on
first use). Start with `./postmakr start` and open http://127.0.0.1:4711.
`./postmakr start --handy` additionally serves HTTPS on your LAN (port 4712)
with a local certificate authority and an access code shown as a QR code,
so a phone can use the camera recorder. Anyone on your Wi-Fi who has the
code can reach the dashboard, so keep it private. `./postmakr mcp` starts
the MCP server for Claude (see `mcp_server/README.md`). Data lives in
`~/postmakr` (override with `POSTMAKR_DATEN`, workspaces live in `workspaces/` below it). License: AGPL-3.0.
