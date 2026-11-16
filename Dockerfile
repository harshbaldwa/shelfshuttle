FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/app/data
WORKDIR /app
RUN install -d -m 0700 /app/data /app/intake \
    && mkdir -p /media/books/audiobooks /media/books/books /downloads
COPY pyproject.toml README.md LICENSE ./
COPY app/ app/
RUN pip install --no-cache-dir .
COPY templates/ templates/
COPY static/ static/
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
    CMD ["python", "-IB", "-m", "app.core.container_entrypoint", "--healthcheck"]
ENTRYPOINT ["python", "-IB", "-m", "app.core.container_entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
