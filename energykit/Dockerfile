ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.22
FROM ${BUILD_FROM}
RUN apk add --no-cache curl git unzip xz jq
WORKDIR /app
COPY app/ /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
