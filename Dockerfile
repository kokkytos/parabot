# syntax=docker/dockerfile:1

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into an isolated prefix so the final image stays clean
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user required by Cloud Run best practices
RUN addgroup --system parabot && adduser --system --ingroup parabot parabot

WORKDIR /app

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY bot.py .

# Create the avatars cache directory and give the non-root user ownership.
# bot.py calls os.makedirs("avatars", exist_ok=True) at startup, but the
# non-root user needs write access to /app for that to succeed.
RUN mkdir -p /app/avatars && chown -R parabot:parabot /app

# Drop to non-root
USER parabot

# Cloud Run injects $PORT at runtime (default 8080).
# The bot reads this variable, so no EXPOSE hard-coding is needed,
# but documenting the default helps local docker run invocations.
EXPOSE 8080

CMD ["python", "bot.py"]
