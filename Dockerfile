FROM python:3.11-slim

WORKDIR /app

# System deps for MCP servers (Node.js + uv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv

# Install Python deps
COPY pyproject.toml .
RUN pip install -e ".[memory]"

# Copy source
COPY . .

CMD ["python", "-m", "kronos"]
