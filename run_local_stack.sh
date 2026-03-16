#!/usr/bin/env bash

set -euo pipefail

# Build and start the full local stack with one command.
cd "$(dirname "$0")"
mkdir -p docker/fil-cache docker/postgres-data secrets
docker compose up --build "$@"