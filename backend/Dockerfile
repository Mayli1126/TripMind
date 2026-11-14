FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /app/backend/data /opt/uv-cache \
    && chown -R app:app /app /opt/uv-cache
COPY --chown=app:app backend/app ./app
COPY --chown=app:app backend/scripts ./scripts
COPY --chown=app:app backend/mcp-overrides.txt ./mcp-overrides.txt
USER app
EXPOSE 8000
# 现有 Worker 随应用启动；本地 SQLite 模式先保持单 API 进程。
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
