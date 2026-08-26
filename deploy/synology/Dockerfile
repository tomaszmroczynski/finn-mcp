# finn-mcp — obraz dla Synology.
#
# To jest Dockerfile UPSTREAMU (github.com/aHk-coder/finn-mcp) z dwoma dodatkami,
# oznaczonymi ponizej jako WARSTWA WDROZENIOWA. Reszta jest przepisana bez zmian,
# zeby aktualizacja upstreamu sprowadzala sie do porownania tych dwoch plikow.
#
# Oba dodatki dzialaja w etapie `builder`, na /opt/venv, ktory etap koncowy tylko
# kopiuje. To jedyne miejsce, w ktorym mozna cokolwiek zmienic: kontener docelowy
# ma `read_only: true` i chodzi jako uzytkownik bez uprawnien.

FROM python:3.11-slim AS builder

WORKDIR /build

# --- WARSTWA WDROZENIOWA 1/2: sufit wersji zaleznosci -------------------------
# Upstream deklaruje "mcp[cli]>=1.27.0" bez gornego ograniczenia. Wydanie 2.x
# wyprowadzilo FastMCP do osobnego pakietu, wiec `mcp.server.fastmcp` przestal
# istniec i kontener wpadal w petle restartow (z zewnatrz: 502 z Cloudflare).
# PIP_CONSTRAINT dziala NA ZEWNATRZ cudzego pyproject.toml, wiec przezywa
# aktualizacje zrodel — w odroznieniu od recznej edycji ich pliku.
COPY constraints.txt /build/constraints.txt
ENV PIP_CONSTRAINT=/build/constraints.txt
# -----------------------------------------------------------------------------

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir .

# --- WARSTWA WDROZENIOWA 2/2: wlasna domena w allowed_hosts ------------------
# SDK ma ochrone przed DNS rebinding i odrzuca zadania z nieznanym naglowkiem
# `Host`, oddajac 421 Invalid Host header. Za proxy `Host` to nasza domena.
# Nazwa hosta przychodzi z .env przez build arg — nie ma jej w tym pliku ani
# w repozytorium. Pusta wartosc zatrzymuje budowanie.
# Sciezka do pakietu liczona jest z samego pakietu, wiec nie zalezy od wersji
# Pythona ani od ukladu site-packages.
ARG FINN_MCP_ALLOWED_HOST=""
COPY patch_allowed_hosts.py /build/patch_allowed_hosts.py
RUN /opt/venv/bin/python /build/patch_allowed_hosts.py "$FINN_MCP_ALLOWED_HOST" \
      "$(/opt/venv/bin/python -c 'import finn_mcp, pathlib; print(pathlib.Path(finn_mcp.__file__).parent)')"
# -----------------------------------------------------------------------------

FROM python:3.11-slim

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINN_CACHE_DB=/data/cache.sqlite

RUN groupadd --system --gid 10001 finn-mcp \
    && useradd --system --uid 10001 --gid finn-mcp --home /nonexistent finn-mcp \
    && mkdir /data \
    && chown finn-mcp:finn-mcp /data

COPY --from=builder /opt/venv /opt/venv

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',8000),2); s.close()"]

CMD ["uvicorn", "finn_mcp.http_server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
