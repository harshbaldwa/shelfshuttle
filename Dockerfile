FROM python:3.12-slim
ARG SHELFSHUTTLE_PUID=10001
ARG SHELFSHUTTLE_PGID=10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/app/data
WORKDIR /app
RUN test "$SHELFSHUTTLE_PUID" -gt 0 \
    && test "$SHELFSHUTTLE_PGID" -gt 0 \
    && install -d -m 0700 -o "$SHELFSHUTTLE_PUID" -g "$SHELFSHUTTLE_PGID" /app/data /app/intake \
    && install -d -m 0755 -o "$SHELFSHUTTLE_PUID" -g "$SHELFSHUTTLE_PGID" /media/audiobooks /media/ebooks \
    && mkdir /downloads
COPY pyproject.toml README.md LICENSE ./
COPY app/ app/
RUN pip install --no-cache-dir .
COPY templates/ templates/
COPY static/ static/
USER ${SHELFSHUTTLE_PUID}:${SHELFSHUTTLE_PGID}
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
