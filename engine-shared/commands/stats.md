---
description: Performance-Zahlen in meta.yaml eintragen — /stats <archiv-ordner> <werte>
argument-hint: <archiv-ordner> ig_likes=… ig_reach=… ig_saves=… ig_shares=… tiktok_views=…
---

Trage Performance-Zahlen ein. Argumente: $ARGUMENTS

1. **Eintragen:** Finde den Archiv-Ordner (Teilstring-Match auf den Ordnernamen in `archive/` reicht, z.B. „2026-07-10_merz"). Trage die übergebenen Werte in `performance:` der `meta.yaml` ein; nicht genannte Felder bleiben unverändert.

2. **Auswerten:** Vergleiche mit `viral_score_prognose` und den Performance-Zahlen der anderen Archiv-Beiträge (gleiche Format-Art zuerst). Bei auffälliger Über- oder Unterperformance:
   - Kurze Hypothese formulieren (Hook? Format? Thema? Timing? Slide 2 zu dicht?).
   - Hypothese ins Feld `learnings:` der meta.yaml schreiben.
   - Wenn daraus eine verallgemeinerbare Regel folgt: Ergänzung für `context/learnings.md` vorschlagen, **nur nach Bestätigung schreiben**.
