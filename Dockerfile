# ─── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for Playwright + crawl4ai
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="ManusClaw"
LABEL org.opencontainers.image.description="Autonomous AI Agent Framework by The-JDdev (SHS Shobuj)"
LABEL org.opencontainers.image.source="https://github.com/The-JDdev/ManusClaw"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /manusclaw

# Runtime system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git sudo ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY . .

# Create workspace and logs directories
RUN mkdir -p workspace/output workspace/.memory workspace/.sessions logs

# Non-root user for safety
RUN useradd -m -s /bin/bash manusclaw && \
    chown -R manusclaw:manusclaw /manusclaw
USER manusclaw

# Default: interactive CLI agent
ENTRYPOINT ["python", "main.py"]
CMD []

# WebSocket server port
EXPOSE 8765

# Healthcheck for server mode
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8765/healthz || exit 1
