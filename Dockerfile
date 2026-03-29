FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY addon.py run.py .
COPY mapping.example.json /app/mapping.example.json

# 默认 config/mapping.json；Linux 若无法解析 host.docker.internal 请在 mapping 里改 upstream
RUN useradd --create-home --uid 10001 mitm \
    && mkdir -p /app/config/mitm \
    && printf '%s\n' '{"version":1,"proxy":{"upstream":"http://host.docker.internal:8096"},"pathRules":[]}' > /app/config/mapping.json \
    && chown -R mitm:mitm /app/config

USER mitm

EXPOSE 8080

VOLUME ["/app/config"]

ENTRYPOINT ["python3", "/app/run.py"]
