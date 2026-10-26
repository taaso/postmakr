# Fremde Bestandteile und ihre Lizenzen

postmakr lokal selbst steht unter der GNU Affero General Public License 3.0
(siehe `LICENSE`). Diese Datei listet alles, was von anderen stammt: was im
Repository mitgeliefert wird, und was erst bei der Installation oder beim
ersten Gebrauch aus dem Netz kommt.

## Mitgeliefert

| Bestandteil | Ort | Lizenz | Lizenztext |
|---|---|---|---|
| Inter (Schrift, variable TTF, aufrecht und kursiv) | `assets/fonts/Inter*.ttf` | SIL Open Font License 1.1 | `assets/fonts/OFL-Inter.txt` |
| Source Sans 3 (Schrift, variable TTF, aufrecht und kursiv) | `assets/fonts/SourceSans3*.ttf` | SIL Open Font License 1.1, Reserved Font Name "Source" | `assets/fonts/OFL-SourceSans3.txt` |
| DM Sans (Schrift, WOFF2, 400/500/700) | `platform_app/static/fonts/dm-sans-*.woff2` | SIL Open Font License 1.1 | `licenses/OFL-DMSans.txt` |
| DM Mono (Schrift, WOFF2, 400/500) | `platform_app/static/fonts/dm-mono-*.woff2` | SIL Open Font License 1.1 | `licenses/OFL-DMMono.txt` |
| YuNet Gesichtserkennung (`face_detection_yunet_2023mar.onnx`, aus dem OpenCV Zoo) | `platform_app/modelle/` | MIT, Copyright (c) 2020 Shiqi Yu | `licenses/MIT-YuNet.txt` |

Geprüft: Die Lizenzangaben in den Namenstabellen der DM-Schriften verweisen auf
die OFL, die Copyright-Zeilen stimmen mit den beiliegenden Lizenztexten
überein. Die Schriften dürfen nach OFL mitgeliefert werden, solange der
Lizenztext beiliegt und sie nicht allein verkauft werden.

Nicht fremd, obwohl der Name es vermuten lässt:
`engine-shared/tools/render_static/animate.min.css` ist eine eigene
Implementierung mit derselben Klassen-Schreibweise wie eine bekannte
Animations-Bibliothek, aber ohne deren Code (siehe
`animate.min.css.LIZENZ.md`). Sie steht unter der AGPL-3.0.

## Beim Rendern aus dem Netz

Einige Slide-Vorlagen in `engine-shared/tools/render_static/templates/` binden
die Zierschriften Anton, Archivo Black und Caveat über Google Fonts ein
(alle SIL Open Font License 1.1). Sie werden nicht mitgeliefert. Ohne
Internet fällt der Renderer auf die mitgelieferten Schriften zurück. Die
Länderflaggen in einigen Vorlagen kommen von flagcdn.com.

## Bei der Installation (nicht mitgeliefert)

Die Python-Pakete aus `requirements.txt`, `requirements-mcp.txt` und
`requirements-extras.txt` installiert `./postmakr installieren` aus dem
Python Package Index. Sie behalten ihre eigenen Lizenzen. Besonders zu nennen:

| Paket | Wofür | Lizenz |
|---|---|---|
| earshot (festgelegter Stand aus github.com/Leonxlnx/earshot) | Hörcheck | MIT |
| faster-whisper | Transkripte, Wortzeiten im Hörcheck | MIT |
| Playwright und der dazugehörige Chromium | Slide-Renderer | Apache-2.0 (Playwright), Chromium BSD-artig mit Bestandteilen unter weiteren Lizenzen |
| opencv-python-headless | Gesichtserkennung im Materiallager | Apache-2.0 |
| torch, transformers, sentence-transformers | Suche nach Bild und Bedeutung | BSD-3-Clause, Apache-2.0, Apache-2.0 |

ffmpeg wird nicht mitgeliefert, du installierst es selbst über deinen
Paketmanager. Es steht je nach Build unter LGPL oder GPL.

## Modelle, die beim ersten Gebrauch geladen werden (nicht mitgeliefert)

Die Modelle kommen beim ersten Gebrauch vom Hugging Face Hub und liegen danach
im Cache auf deinem Rechner (Standard `~/.cache/huggingface`, die Hörcheck-
Modelle von earshot unter `EARSHOT_HOME`). Lizenz laut Modellkarte, Stand
Oktober 2026:

| Modell | Wofür | Lizenz |
|---|---|---|
| intfloat/multilingual-e5-small | Suche nach Bedeutung (Texte) | MIT |
| google/siglip-base-patch16-256-multilingual | Suche nach Bild | Apache-2.0 |
| laion/clap-htsat-unfused | Suche nach Klang | Apache-2.0 |
| laion/larger_clap_general | Hörcheck (earshot) | Apache-2.0 |
| MIT/ast-finetuned-audioset-10-10-0.4593 (AST) | Hörcheck (earshot), Klang-Etiketten | BSD-3-Clause |
| Systran/faster-whisper-small (Whisper small) | Transkripte und Wortzeiten | MIT |

Die Lizenzen gelten für die Modellgewichte. Die Trainingsdaten der Klang-
Modelle (unter anderem AudioSet und LAION-Audio-630K) haben eigene
Nutzungsbedingungen. Wenn du Ergebnisse kommerziell nutzt, lies die
Modellkarten selbst.
