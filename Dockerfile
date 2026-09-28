FROM python:3.13-slim-bookworm

ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/ proxy/
COPY web/ web/

RUN mkdir -p /app/config

EXPOSE 8080 8081
ENTRYPOINT ["python3", "/app/proxy/run.py"]
