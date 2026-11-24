# Virtuozzo Application Management Assistant — MCP server + agent backend in one image.
#
# Build (from the repository root):
#   docker build -t vap-copilot:0.1.0 -f deploy/Dockerfile .
# Run:
#   docker run --rm -p 8080:8080 \
#     -e VAP_LLM_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1 \
#     -e VAP_LLM_MODEL=mimo-v2.6-pro \
#     -e VAP_LLM_API_KEY="$VAP_LLM_API_KEY" \
#     vap-copilot:0.1.0
#
# The container serves:
#   POST /mcp      — MCP Streamable HTTP (per-request X-Jelastic-* auth)
#   /api/*, /      — agent backend (when agent/ is present in the build)
#   GET /healthz   — liveness
#
# The LLM API key is deployment-time configuration: pass it as an env var at
# run time (or the platform's container env), never bake it into the image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VAP_SERVE_HOST=0.0.0.0 \
    VAP_SERVE_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp/ mcp/
COPY agent/ agent/
COPY mock/ mock/
COPY deploy/ deploy/

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "deploy.serve"]
