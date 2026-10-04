FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY ragx ./ragx
RUN pip install ".[postgres]" && useradd --create-home ragx && mkdir -p /app/data && chown ragx /app/data

USER ragx
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "ragx.api:app", "--host", "0.0.0.0", "--port", "8000"]
