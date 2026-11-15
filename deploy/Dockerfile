FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bubblewrap openssh-client antiword \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ ./server/
COPY agents/ ./agents/
# macp-signer runs `uvicorn signer.app:app` from this same image.
COPY signer/ ./signer/
COPY --from=web /web/dist ./web
EXPOSE 8000
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
