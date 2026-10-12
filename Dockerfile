FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SNIPE_DB_PATH=/data/snipe.db
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snipe ./snipe
COPY config.example.yaml ./

RUN useradd --create-home snipe && mkdir /data && chown snipe /data
USER snipe
VOLUME /data

CMD ["python", "-m", "snipe"]
