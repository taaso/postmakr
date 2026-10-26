#!/usr/bin/env python3
"""Transkription mit faster-whisper (CPU). Audio- oder Videodatei rein,
Text, SRT oder Wort-JSON raus. Modelle werden beim ersten Lauf nach HF_HOME
geladen (Standard ~/.cache/huggingface, bleibt dort liegen).

Aufruf:
  python3 engine-shared/tools/transcribe.py <datei> [--model small] [--lang de]
        [--format txt|srt|json] [--out <datei>]

Modelle: tiny, base, small (Standard, gutes Deutsch bei CPU-Tempo), medium.
Auf 8 Kernen schafft small etwa Echtzeit-Faktor 0.3 (10 min Audio in 3 min).
"""

import argparse
import json
import sys
from pathlib import Path


def fmt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("datei")
    ap.add_argument("--model", default="small")
    ap.add_argument("--lang", default=None, help="Sprachcode (de, en). Leer = automatisch erkennen")
    ap.add_argument("--format", choices=["txt", "srt", "json"], default="txt")
    ap.add_argument("--out", default=None, help="Zieldatei (Standard: stdout)")
    ap.add_argument("--threads", type=int, default=0, help="CPU-Threads (0 = alle)")
    args = ap.parse_args()

    src = Path(args.datei)
    if not src.is_file():
        sys.exit(f"Datei nicht gefunden: {src}")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper fehlt: pip install faster-whisper")

    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)
    segments, info = model.transcribe(
        str(src), language=args.lang, vad_filter=True,
        word_timestamps=(args.format == "json"),
    )
    segs = list(segments)

    if args.format == "txt":
        out = "\n".join(s.text.strip() for s in segs) + "\n"
    elif args.format == "srt":
        lines = []
        for i, s in enumerate(segs, 1):
            lines += [str(i), f"{fmt_ts(s.start)} --> {fmt_ts(s.end)}", s.text.strip(), ""]
        out = "\n".join(lines)
    else:
        out = json.dumps({
            "sprache": info.language, "wahrscheinlichkeit": round(info.language_probability, 3),
            "dauer": round(info.duration, 2), "modell": args.model,
            "segmente": [{
                "start": round(s.start, 2), "ende": round(s.end, 2), "text": s.text.strip(),
                "woerter": [{"w": w.word, "start": round(w.start, 2), "ende": round(w.end, 2)} for w in (s.words or [])],
            } for s in segs],
        }, ensure_ascii=False, indent=1)

    if args.out:
        Path(args.out).write_text(out)
        print(f"{len(segs)} Segmente, Sprache {info.language} ({info.language_probability:.0%}) → {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
