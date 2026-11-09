FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC
EXPOSE 8080
CMD ["python", "-m", "cctv.server", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/cctv.db"]
