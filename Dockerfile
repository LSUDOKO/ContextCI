FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Configured entirely through the environment; see README.md.
#   GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER
#   DATAHUB_MCP_URL (or DATAHUB_GMS_URL), DATAHUB_GMS_TOKEN
#   ANTHROPIC_API_KEY
ENTRYPOINT ["python", "-m", "src.main"]
