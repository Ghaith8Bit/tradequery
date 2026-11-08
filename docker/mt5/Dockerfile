# MetaTrader 5 + Windows Python running under Wine, so the Windows-only MetaTrader5
# library works on Linux (WSL2). Build context is the repository root.
#
# The big downloads (Windows Python, the MT5 terminal) happen once, on `setup`, into
# the `mt5-wine` volume, not at image build time. The image itself stays small.
FROM debian:bookworm-slim

ARG UID=1000
ENV DEBIAN_FRONTEND=noninteractive \
    WINEARCH=win64 \
    WINEPREFIX=/home/trader/.wine \
    WINEDEBUG=-all \
    DISPLAY=:99

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates wget gnupg2 xvfb xauth cabextract winbind procps \
 && mkdir -pm755 /etc/apt/keyrings \
 && wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
 && wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources \
 && apt-get update \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid ${UID} trader \
 && mkdir -p /data "$WINEPREFIX" \
 && chown -R trader /data "$WINEPREFIX"

COPY --chown=trader pyproject.toml README.md /src/
COPY --chown=trader src /src/src
COPY docker/mt5/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV TQ_DATA_DIR='Z:\data'
USER trader
ENTRYPOINT ["entrypoint.sh"]
CMD ["help"]
