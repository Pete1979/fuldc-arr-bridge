FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/Pete1979/fuldc-arr-bridge"
LABEL org.opencontainers.image.description="Request movies & TV in Seerr/Jellyseerr/Overseerr and auto-download them over Direct Connect via FulDC++"
LABEL org.opencontainers.image.licenses="MIT"

# Not every log line goes through flush=True, and a buffered stdout means those
# never reach `docker logs` at all — including the record of what was searched.
ENV PYTHONUNBUFFERED=1

# stdlib-only app — no pip install needed
WORKDIR /app
# webhook flow + CLI
COPY fuldc_client.py ranker.py core.py httputil.py notify.py plex.py metadata.py tvmaze.py library.py season_monitor.py webhook_server.py bridge.py ./
# Radarr/Sonarr flow (arr_server.py, run via the "arr" compose profile)
COPY arr_server.py torznab.py qbit.py store.py ./

USER 1000
EXPOSE 8080 9117
CMD ["python", "webhook_server.py"]
