# StormCell — image de production minimale.
# Build : docker build -t stormcell .
# Run   : docker run -p 8000:8000 -v stormcell-data:/data stormcell
FROM python:3.12-slim

WORKDIR /app

# Dépendances d'abord (cache de build)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Config par défaut (surchargeable par un volume monté sur /app/config.toml)
COPY config.toml ./

# La base SQLite vit dans /data (à monter en volume pour la persistance).
ENV BLITZ_DB=/data/lightning_log.db
RUN mkdir -p /data

EXPOSE 8000
# host 0.0.0.0 pour être joignable depuis l'extérieur du conteneur
CMD ["python", "-m", "blitz", "web", "--host", "0.0.0.0", "--port", "8000"]
