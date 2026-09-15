ARG JELLYFIN_BASE_TAG=latest
FROM jellyfin/jellyfin:${JELLYFIN_BASE_TAG}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER root

RUN apt-get update \
    && apt-get install --no-install-recommends --no-install-suggests --yes \
        python3 \
        python3-requests \
    && apt-get clean autoclean --yes \
    && apt-get autoremove --yes \
    && rm -rf /var/cache/apt/archives* /var/lib/apt/lists/*

WORKDIR /opt/progress-sync

COPY app.py ./app.py
COPY community.py ./community.py
COPY security_agent.py ./security_agent.py
COPY static ./static
COPY community ./community
COPY docker/install-community-web.py ./install-community-web.py
COPY config.example.json ./config.example.json
COPY security-alerts.example.json ./security-alerts.example.json
COPY docker/entrypoint-with-progress-sync.sh /entrypoint-with-progress-sync.sh

RUN python3 ./install-community-web.py
RUN chmod 755 /entrypoint-with-progress-sync.sh

EXPOSE 8097

ENTRYPOINT ["/entrypoint-with-progress-sync.sh"]
