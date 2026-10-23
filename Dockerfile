# Image de base épinglée (évite qu'un "slim" flottant change de version Debian sous tes pieds)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TTS_CACHE_DIR=/var/cache/tts \
    TTS_CONFIG=/config/pieces.yml \
    TTS_BENCH_OUT=/bench-out

# Dépendances système : paplay (pulseaudio-utils) et tzdata (pour que TZ=Europe/Paris soit pris en compte)
RUN apt-get -yq update && \
    DEBIAN_FRONTEND=noninteractive apt-get -yq install --no-install-recommends pulseaudio-utils tzdata && \
    apt-get -yq clean && \
    rm -rf /var/lib/apt/lists/*

# Utilisateur non-root : son UID doit être propriétaire des dossiers ./cache, ./models et ./bench_out
# montés depuis l'hôte (seul ./cache est écrit en fonctionnement normal)
ARG UID=1000
RUN useradd -m -u ${UID} text2speak && \
    mkdir -p /var/cache/tts /models /bench-out && chown text2speak /var/cache/tts /models /bench-out

WORKDIR /text2speak

# Dépendances Python d'abord : cette couche reste en cache tant que requirements.txt ne change pas
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY tts_engines.py tts_server.py bench_tts.py ./

USER text2speak

EXPOSE 1337
VOLUME /var/cache/tts

# Le chargement d'un modèle de synthèse sur un petit processeur peut prendre plusieurs secondes
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1337/cache', timeout=3)"

CMD ["python", "/text2speak/tts_server.py"]
# EoF
