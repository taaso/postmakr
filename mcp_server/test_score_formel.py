"""Test für die feste Viral-Score-Formel (mcp_server.workflows.viral_score).

Läuft ohne pytest: `python3 mcp_server/test_score_formel.py`. Exit-Code 0 bei
Erfolg, 1 bei einem fehlgeschlagenen Assert.
"""

from __future__ import annotations

import sys

from workflows import viral_score


def test_alle_kriterien_voll_erfuellt_ergibt_10() -> None:
    assert viral_score([(2, 5), (2, 3), (2, 1)]) == 10


def test_alle_kriterien_null_ergibt_1() -> None:
    assert viral_score([(0, 5), (0, 3), (0, 1)]) == 1


def test_gemischte_bewertung() -> None:
    # Gewichte 5+3+1=9, max 18. Summe = 2*5 + 1*3 + 0*1 = 13.
    # score = 1 + 9 * (13/18) = 1 + 6.5 = 7.5 -> rundet auf 8 (Python round-half-to-even -> 8).
    assert viral_score([(2, 5), (1, 3), (0, 1)]) == 8


def test_ein_einzelnes_kriterium_halb_erfuellt() -> None:
    # score = 1 + 9 * (1*4 / (2*4)) = 1 + 9*0.5 = 5.5 -> round() = 6 (Banker's rounding: 5.5 -> 6).
    assert viral_score([(1, 4)]) == round(5.5)


def test_leere_kriterienliste_ergibt_none() -> None:
    assert viral_score([]) is None


def test_punkte_werden_auf_0_bis_2_geklemmt() -> None:
    # 5 Punkte auf einem Kriterium mit Gewicht 2 wird wie 2 Punkte behandelt -> voll = 10.
    assert viral_score([(5, 2)]) == 10
    # negative Punkte werden wie 0 behandelt -> minimal = 1.
    assert viral_score([(-3, 2)]) == 1


def test_score_liegt_immer_zwischen_1_und_10() -> None:
    for punkte in range(0, 3):
        for gewicht in range(1, 6):
            s = viral_score([(punkte, gewicht)])
            assert s is not None
            assert 1 <= s <= 10, f"score {s} außerhalb 1..10 bei ({punkte},{gewicht})"


def main() -> int:
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} Tests grün.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
