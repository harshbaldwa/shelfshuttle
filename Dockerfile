FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/app/data
WORKDIR /app
RUN groupadd -g 10001 shelfshuttle \
    && useradd --no-create-home -u 10001 -g 10001 shelfshuttle \
    && install -d -m 0700 -o 10001 -g 10001 /app/data /app/intake \
    && install -d -m 0755 -o 10001 -g 10001 /media/audiobooks /media/ebooks \
    && mkdir /downloads
COPY pyproject.toml README.md LICENSE ./
COPY app/ app/
RUN pip install --no-cache-dir .
COPY templates/ templates/
COPY static/ static/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
