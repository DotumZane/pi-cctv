FROM python:3.12-slim

WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "cctv.server", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/cctv.db"]
