# Serveur Codawar Pixel Studio — image légère (fonctionne sur Raspberry Pi 4 / arm64).
FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] embarque libpq (roues disponibles pour amd64 et arm64).
RUN pip install --no-cache-dir "psycopg[binary]==3.3.9"

COPY server.py index.html ./

ENV HOST=0.0.0.0 \
    PORT=5055 \
    PYTHONUNBUFFERED=1

EXPOSE 5055

CMD ["python", "server.py"]
