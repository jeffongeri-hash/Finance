FROM python:3.11-slim

# System deps needed for some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy entire repo
COPY . .

# ── Core deps (always required) ───────────────────────────────────────────────
RUN pip install --no-cache-dir \
    fastapi==0.115.5 \
    "uvicorn[standard]==0.32.1" \
    httpx==0.27.2 \
    pandas==2.2.3 \
    numpy==2.0.2 \
    python-dotenv==1.0.1 \
    pydantic==2.10.3 \
    pydantic-settings==2.6.1 \
    aiohttp==3.11.10 \
    websockets==14.1 \
    fredapi==0.5.2 \
    finnhub-python==2.4.20 \
    colorama==0.4.6 \
    tabulate==0.9.0 \
    python-dateutil \
    rich \
    questionary \
    matplotlib

# yfinance pinned — install core first then yfinance without overriding deps
RUN pip install --no-cache-dir requests lxml beautifulsoup4 html5lib multitasking && \
    pip install --no-cache-dir "yfinance==0.2.50"

# ── Optional deps (build failures are non-fatal) ──────────────────────────────
RUN pip install --no-cache-dir "nasdaq-data-link==1.0.4" || echo "nasdaq-data-link skipped"
RUN pip install --no-cache-dir "py-clob-client>=0.16.0" || echo "py-clob-client skipped"
RUN pip install --no-cache-dir \
    "langchain>=0.3.7" \
    "langgraph>=0.2.56" \
    "langchain-core>=0.3.0" \
    "langchain-openai>=0.3.5" \
    "langchain-anthropic>=0.3.5" \
    "langchain-groq>=0.2.3" || echo "langchain packages skipped"

# ── Runtime env ───────────────────────────────────────────────────────────────
# ai-hedge-fund is at /app/ai-hedge-fund relative to repo root
ENV AI_HEDGE_FUND_PATH=/app/ai-hedge-fund
ENV PYTHONUNBUFFERED=1

WORKDIR /app/market-catalyst-tracker/backend

EXPOSE 8000

# Railway injects $PORT; fall back to 8000 locally
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
