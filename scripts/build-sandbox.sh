#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")/docker/sandbox"

echo "Building sandbox Docker image..."
docker build -t kronos-sandbox:latest "$DOCKER_DIR"
echo "Done. Image: kronos-sandbox:latest"
