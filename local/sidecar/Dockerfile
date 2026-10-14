FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --create-home --uid 10001 appuser

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Named volume ownership is taken from this directory in the image, so it must
# be owned by the runtime user here or the sidecar cannot open its database.
RUN mkdir -p /data && chown appuser:appuser /data
ENV SIDECAR_DB=/data/sidecar.db
VOLUME ["/data"]

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
