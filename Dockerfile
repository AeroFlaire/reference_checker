FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    UPLOAD_FOLDER=/data/uploads \
    CACHE_DB=/data/cache/api_cache.db \
    RESULTS_DB=/data/results/results.db

# Create the persistent dirs (real data lives in the mounted /data volume)
RUN mkdir -p /data/uploads /data/cache /data/results

EXPOSE 5000

CMD ["python", "app.py"]
