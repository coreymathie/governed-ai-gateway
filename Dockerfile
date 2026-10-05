# Governed AI Gateway: the FastAPI gateway plus the console (demo/ served at /console/).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY router ./router
COPY config ./config
COPY demo ./demo
COPY dashboard ./dashboard
COPY evals ./evals
COPY policies ./policies

RUN useradd --create-home --uid 10001 gateway && mkdir -p /data && chown gateway /data && chown -R gateway /app/config
USER gateway

EXPOSE 4000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4000/health', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "4000"]
