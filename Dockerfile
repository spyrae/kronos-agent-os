FROM python:3.11-slim

WORKDIR /app

# System deps for MCP servers (Node.js + uv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv

# Copy package source before editable install. `pip install -e` needs the
# package directory to exist during the build.
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY kronos ./kronos
COPY dashboard ./dashboard
COPY aso ./aso
# Keep the public quickstart image lightweight. The memory extra pulls the
# local embedding stack (Torch) and is intentionally opt-in for custom images.
RUN pip install --no-cache-dir -e .

# Copy remaining public repo files such as docs, examples, and templates.
COPY . .

# Build the dashboard UI inside the image. The build output is served by
# dashboard.server when dashboard-ui/dist exists.
RUN cd dashboard-ui \
    && npm ci \
    && npm run build \
    && rm -rf node_modules

CMD ["kaos", "dashboard"]
