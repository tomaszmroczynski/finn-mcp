# finn-mcp — warstwa wdrozeniowa (Synology)

Ten katalog nie jest kopia serwera. Serwer to reszta tego repozytorium —
forka `github.com/aHk-coder/finn-mcp`. To jest **warstwa wdrozeniowa nad nim**:
wszystko, co musi byc prawda po naszej stronie, zeby kontener wstawal
i odpowiadal — zapisane w plikach, ktore da sie zacommitowac.

Do 25 sierpnia 2026 lezala w `ripper-skills-tools/tools/finn-mcp-synology`,
obok wciagnietej kopii zrodel. Kopia zostala usunieta, bo zaczela sie rozjezdzac
z forkiem; warstwa wdrozeniowa przeniosla sie tutaj, zeby stac przy kodzie,
ktory wdraza.

---

## ⚠ Zanim ruszysz NAS — przeczytaj do konca

Stan na 8 wrzesnia 2026:

- **Klient jest juz poprawiony i wdrozony** (`ripperdoc-strona`, commit `7c9e3cd`):
  `asArray` w `lib/finn/jobs.ts` czyta oba ksztalty odpowiedzi `search_finn` —
  stara liste i nowy obiekt z `results`. Warunek „najpierw strona, potem
  kontener" jest dotrzymany.
- **`src/finn_mcp/http_server.py` istnialo wylacznie na NAS-ie.** To warstwa
  HTTP z tokenem, bez ktorej kontener nie wstaje (`CMD` w Dockerfile). Jest juz
  w forku, bajt w bajt, razem z `tests/test_http_server.py`. Domena
  z `allowed_hosts` przeszla z recznie edytowanego `server.py` do
  `http_server.py` i przychodzi z `FINN_MCP_ALLOWED_HOST` w czasie dzialania.
- **Na NAS-ie sa jeszcze inne pliki z recznymi zmianami spoza gita.** Dziewiec
  plikow utwardzenia nie pasuje do zadnego commitu. Dopoki nie porownane
  z forkiem plik po pliku (patrz „Inwentaryzacja" nizej), **nie nadpisuj `src/`**.

## Inwentaryzacja przed nadpisaniem

To jest krok, ktorego brak kosztowalby kontener. Katalog na NAS-ie nie jest
klonem gita — to kopia, w ktorej przez tygodnie robilo sie poprawki na zywo.
Kazda z nich istnieje w jednym egzemplarzu i znika przy pierwszym `cp -r`.

Zanim cokolwiek nadpiszesz, na NAS-ie:

```sh
cd /volume1/docker/finn-mcp
find . \( -path ./data -o -name __pycache__ \) -prune -o -type f -print | sort
for f in $(find src tests -name '*.py'); do printf '%s  %s
' "$(tr -d '' < "$f" | sha256sum | cut -d' ' -f1)" "$f"; done
```

**`tr -d ''` jest tu istotne.** Pliki trafily na NAS z Windowsa i maja CRLF;
bez zdjecia CR kazdy skrot rozni sie od gita i wszystko wyglada na zmienione.
Po stronie repozytorium ten sam skrot daje `git show <commit>:<sciezka> | sha256sum`.

Regula: plik, ktorego skrot nie pasuje do zadnego commitu, **czytasz przed
nadpisaniem** i — jesli niesie cos wartosciowego — najpierw commitujesz do
forka. Dopiero wtedy fork jest kompletnym zrodlem i mozna z niego wdrazac.

## Po co to istnieje

Dwie poprawki, ktore utrzymywaly ten serwer przy zyciu, siedzialy do 5 sierpnia
w recznie zmienionej kopii cudzego projektu na NAS-ie:

1. **Sufit wersji `mcp[cli]<2`.** Upstream deklaruje `>=1.27.0` bez gornego
   ograniczenia. Wydanie 2.x wyprowadzilo FastMCP do osobnego pakietu, wiec
   `mcp.server.fastmcp` przestal istniec i kontener wpadal w petle restartow.
   Z zewnatrz wygladalo to na 502 z Cloudflare.

2. **Domena w `allowed_hosts`.** SDK ma ochrone przed DNS rebinding i odrzuca
   zadania z nieznanym naglowkiem `Host`, oddajac `421 Invalid Host header`.
   Za proxy `Host` to nasza domena, ktorej serwer nie zna.

Obie poprawki znikaja przy pierwszej aktualizacji upstreamu. Wtedy awaria wraca,
a diagnoza zaczyna sie od zera — bo nigdzie nie ma sladu, ze cokolwiek bylo
poprawiane. Ten katalog jest tym sladem.

## Co tu jest

| plik | rola |
|---|---|
| `Dockerfile` | Dockerfile upstreamu z DWOMA dodatkami, oznaczonymi `WARSTWA WDROZENIOWA` |
| `compose.yaml` | compose upstreamu z jedna zmiana: przekazaniem nazwy hosta do budowania |
| `constraints.txt` | sufit wersji dla pipa, dzialajacy NA ZEWNATRZ cudzego `pyproject.toml` |
| `patch_allowed_hosts.py` | dopisuje nasza domene do `allowed_hosts` w ZAINSTALOWANYM pakiecie |
| `.env.example` | ksztalt pliku `.env`; wartosci wylacznie na NAS-ie i w menedzerze hasel |

Ani nazwa hosta, ani token nie sa wpisane w zaden z tych plikow. Obie wartosci
przychodza z `.env`, ktorego tu nie ma i nie bedzie.

## Dlaczego akurat tak

**Sufit wersji przez `PIP_CONSTRAINT`, a nie przez edycje `pyproject.toml`.**
Cudzy plik nadpisze sie przy pierwszej aktualizacji zrodel. Plik ograniczen
lezy obok i dziala niezaleznie od tego, co upstream napisze u siebie.

**Latka szuka pliku sama.** Skrypt dostaje KATALOG pakietu, przeglada go
i znajduje miejsce, w ktorym powstaje `TransportSecuritySettings`. Podanie
sciezki na sztywno oznaczaloby, ze przeniesienie tego kodu do innego modulu
zamienia latke w cicha atrape: build przechodzi, obraz wstaje, a produkcja
oddaje 421. Skrypt w takiej sytuacji **przerywa budowanie**.

I to nie jest ostroznosc teoretyczna: aplikacje uruchamia `finn_mcp.http_server`
(patrz CMD w Dockerfile), wiec naturalne zalozenie bylo takie, ze ustawienia
transportu siedza wlasnie tam. Sprawdzenie na zbudowanym obrazie (5 sierpnia)
pokazalo co innego — zalatany zostal `finn_mcp/server.py`. Sciezka wpisana
na sztywno bylaby wtedy pusta, a latka nie zrobilaby nic.

**Obie zmiany w etapie `builder`.** Kontener docelowy ma `read_only: true`
i chodzi jako uzytkownik 10001 bez uprawnien — po starcie nie da sie w nim juz
niczego zmienic, i tak ma byc.

## Wdrozenie

Zrodla przychodza z forka, a katalog na NAS-ie sie **naklada**, nie zastepuje:
`.env`, `data/` (baza z zapisanymi wyszukiwaniami) i `.dockerignore` zostaja.
Najpierw kopia zapasowa — to jedyna droga powrotu, bo NAS nie ma gita.

```sh
cd /volume1/docker/finn-mcp
tar czf /volume1/docker/finn-mcp-backup-$(date +%Y%m%d-%H%M).tgz --exclude=./data .
curl -L https://github.com/tomaszmroczynski/finn-mcp/archive/refs/heads/master.tar.gz | tar xz -C /tmp
cp -r /tmp/finn-mcp-master/src/. ./src/
cp -r /tmp/finn-mcp-master/tests/. ./tests/
cp /tmp/finn-mcp-master/pyproject.toml /tmp/finn-mcp-master/README.md /tmp/finn-mcp-master/LICENSE /tmp/finn-mcp-master/uv.lock .
cp /tmp/finn-mcp-master/deploy/synology/Dockerfile /tmp/finn-mcp-master/deploy/synology/compose.yaml /tmp/finn-mcp-master/deploy/synology/constraints.txt /tmp/finn-mcp-master/deploy/synology/patch_allowed_hosts.py .
sudo docker compose build --no-cache
sudo docker compose up -d --force-recreate
```

Forma `src/.` → `./src/` scala zawartosc do istniejacego katalogu; `cp -r src .`
przy istniejacym `./src` robi to samo, ale zapis z kropka nie zostawia miejsca
na watpliwosc.

**`--no-cache` nie jest ostroznoscia, tylko warunkiem.** Bez niego Docker
zostawia stara warstwe `pip install` i zmiana ograniczen wersji nie ma zadnego
skutku — obraz buduje sie „pomyslnie" i zawiera dokladnie ten sam blad.

**`--force-recreate`** jest potrzebne osobno: zmienne srodowiskowe wczytuja sie
przy starcie kontenera, wiec sam nowy obraz nie wystarczy, zeby kontener
zobaczyl nowy token.

W logu budowania musi pojawic sie linia `patch_allowed_hosts: dopisano …`.
Jesli jej nie ma, latka nie zadzialala i nie ma sensu isc dalej.

Od wrzesnia 2026 latka jest pasem bezpieczenstwa, nie jedyna droga:
`http_server.py` czyta `FINN_MCP_ALLOWED_HOST` w czasie dzialania, a
`compose.yaml` przekazuje te zmienna takze do srodowiska kontenera. Zadna
z dwoch drog nie jest sama jedna, wiec pominiecie jednej nie konczy sie 421.

## Czy na pewno budujesz TYM Dockerfile'em

Najczestszy blad przy wdrozeniu: archiwum trafia gdzie indziej niz katalog
projektu, budowanie idzie po pliku upstreamu i konczy sie sukcesem — tyle ze bez
obu poprawek. Obraz wstaje, a produkcja oddaje 421 albo kontener wpada w petle
restartow przy nastepnej aktualizacji zaleznosci.

Sprawdzenie przed budowaniem, w katalogu projektu:

```sh
cd /volume1/docker/finn-mcp
head -1 Dockerfile
ls -l constraints.txt patch_allowed_hosts.py
cat .dockerignore
```

Pierwsza linia tego Dockerfile'a to komentarz `# finn-mcp — obraz dla Synology.`
Jesli widzisz `FROM python:3.11-slim AS builder`, to nadal lezy tam wersja
upstreamu i archiwum nie zostalo rozpakowane w tym miejscu.

`.dockerignore` sprawdzamy z osobnego powodu: jesli wyklucza wszystko poza
wymienionymi plikami, trzeba dopisac do niego `constraints.txt`
i `patch_allowed_hosts.py` — inaczej `COPY` w budowaniu padnie.

Trzy sygnaly W LOGU budowania, po ktorych poznasz, ze poszlo dobrze:

- `transferring dockerfile:` pokazuje okolo **3 kB**. Plik upstreamu ma ~1 kB,
  wiec „1.02kB" oznacza, ze zbudowal sie tamten.
- etap `builder` ma **wiecej niz piec krokow** (`1/9`…`9/9`). Upstream ma ich
  dokladnie piec — jesli widzisz `1/5`…`5/5`, poprawek nie ma.
- pojawia sie linia **`patch_allowed_hosts: dopisano …`**. To jedyny bezposredni
  dowod, ze latka zadzialala. Jej brak = nie ma sensu isc dalej.

## Sprawdzenie po wdrozeniu

Kontener zyje i nasluchuje:

```sh
sudo docker compose logs --tail=20 finn-mcp   # ostatnia linia: Uvicorn running on http://0.0.0.0:8000
```

Token w kontenerze zgadza sie z tym, ktory ma klient — **bez pokazywania
sekretu**, przez porownanie odciskow:

```sh
sudo docker exec finn-mcp printenv FINN_MCP_ACCESS_TOKEN | tr -d '\n' | sha256sum | cut -c1-12
```

Ta sama liczba po stronie klienta = zgadza sie. Zasada z 1 sierpnia: **pytaj
kontener, nie plik.** `.env` mowi, co ma byc; `printenv` w kontenerze mowi,
co jest.

Caly lancuch od zewnatrz:

```sh
npm run finn:check     # w C:\AI\Ripperdoc\ripperdoc-strona
```

## Aktualizacja upstreamu

Zrodla nie sa juz kopiowane recznie — to repozytorium jest forkiem, wiec
nowsza wersje sciaga sie przez `git fetch upstream && git merge upstream/master`.
Po takim scaleniu: porownaj ICH `Dockerfile` i `compose.yaml` z tutejszymi,
przenies ewentualne zmiany upstreamu i **zostaw oba bloki
`WARSTWA WDROZENIOWA`**. Potem zbuduj z `--no-cache`. Jesli latka przestanie
pasowac, build sie zatrzyma z czytelnym komunikatem — to jest zaplanowane
zachowanie, nie awaria.

## Do zrobienia w upstreamie

Brak gornego ograniczenia `mcp[cli]` to realny blad tamtego projektu, nie naszej
konfiguracji. Dopoki tam siedzi, kazdy, kto zbuduje tamten projekt po wydaniu
2.x, dostanie ta sama awarie — warto to zglosic osobnym zgloszeniem albo PR-em.
Otwarty PR do upstreamu (`aHk-coder/finn-mcp#1`) dotyczy czego innego: odczytu
ogloszen o prace po zniknieciu JSON-LD.
