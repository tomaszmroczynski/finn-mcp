#!/usr/bin/env python3
"""Dopisz wlasna domene do listy dozwolonych hostow MCP (ochrona przed DNS rebinding).

Serwer MCP w SDK Pythona ma warstwe `TransportSecurityMiddleware`, ktora odrzuca
zadania z nieznanym naglowkiem Host i oddaje `421 Invalid Host header`. Za proxy
(Cloudflare -> reverse proxy DSM -> kontener) `Host` to nasza domena, ktorej
serwer nie zna, wiec zdrowy kontener i tak odmawia obslugi.

Skrypt dziala na ZAINSTALOWANYM pakiecie w obrazie (etap `builder`, na /opt/venv),
wiec nie wymaga edycji cudzego repozytorium. Sam ZNAJDUJE plik do zmiany, zamiast
przyjmowac sciezke na wiare — dzieki temu przeniesienie kodu do innego modulu
w upstreamie nie zamienia tej latki w cicha atrape.

KONCZY SIE BLEDEM, gdy nie znajdzie czego szukal. To jest celowe: obraz, ktory
wstaje i odmawia obslugi dopiero na produkcji, jest gorszy niz build, ktory nie
przeszedl.

Uzycie:
    python3 patch_allowed_hosts.py <host> <katalog-pakietu-albo-plik>
Przyklad:
    python3 patch_allowed_hosts.py finn.example.no /opt/venv/lib/python3.11/site-packages/finn_mcp
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MARKERS = ("TransportSecuritySettings", "allowed_hosts")


def patch_source(source: str, host: str) -> str | None:
    """Zwraca zmieniona tresc albo None, gdy nie bylo czego zmienic."""
    if host in source:
        return None  # juz zalatane (powtorny build tej samej warstwy)

    m = re.search(r"allowed_hosts\s*=\s*\[([^\]]*)\]", source)
    if m:
        inside = m.group(1).strip()
        sep = "" if not inside else ", "
        return source[: m.start(1)] + f"{inside}{sep}{host!r}" + source[m.end(1) :]

    m = re.search(r"TransportSecuritySettings\s*\(", source)
    if m:
        return source[: m.end()] + f"allowed_hosts=[{host!r}], " + source[m.end() :]

    return None


def candidates(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise SystemExit(f"patch_allowed_hosts: nie ma pliku ani katalogu {target}")
    hits = [p for p in sorted(target.rglob("*.py")) if any(k in p.read_text(encoding="utf-8", errors="ignore") for k in MARKERS)]
    if not hits:
        raise SystemExit(
            f"patch_allowed_hosts: w {target} nie ma pliku wspominajacego "
            f"{' ani '.join(MARKERS)}. Upstream zmienil ksztalt — zajrzyj do kodu "
            "i popraw ten skrypt, ZANIM obraz pojdzie na produkcje."
        )
    return hits


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"uzycie: {sys.argv[0]} <host> <katalog-pakietu-albo-plik>")
    host, target = sys.argv[1].strip(), Path(sys.argv[2])
    if not host:
        raise SystemExit(
            "patch_allowed_hosts: pusta nazwa hosta. Ustaw FINN_MCP_ALLOWED_HOST "
            "w .env na NAS-ie — bez tego serwer odda 421 na kazde zadanie zza proxy."
        )

    changed: list[Path] = []
    already: list[Path] = []
    for path in candidates(target):
        source = path.read_text(encoding="utf-8")
        if host in source:
            already.append(path)
            continue
        patched = patch_source(source, host)
        if patched is None:
            continue
        path.write_text(patched, encoding="utf-8")
        changed.append(path)

    if changed:
        for p in changed:
            print(f"patch_allowed_hosts: dopisano {host} w {p}")
        return
    if already:
        print(f"patch_allowed_hosts: {host} juz obecny w {already[0]} — nic nie zmieniam")
        return

    raise SystemExit(
        f"patch_allowed_hosts: znalazlem pliki ze wzmianka o {MARKERS[0]}/{MARKERS[1]}, "
        "ale w zadnym nie bylo miejsca do podmiany. Zajrzyj do kodu i popraw ten skrypt."
    )


if __name__ == "__main__":
    main()
