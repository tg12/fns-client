#!/usr/bin/env bash

set -euo pipefail

# Build and start the full local stack with one command.
cd "$(dirname "$0")"
mkdir -p docker/fil-cache docker/postgres-data secrets
mkdir -p docker/opensky-data

if [[ "${1:-}" == "down" ]]; then
	docker compose down --remove-orphans
	exit 0
fi

docker compose up -d --build "$@"

echo "Waiting for API health..."
for _ in {1..60}; do
	if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
		break
	fi
	sleep 1
done

echo "Stack is up"
echo "Dashboard: http://127.0.0.1:8080/"
echo "Aircraft API: http://127.0.0.1:8080/api/aircraft"
echo "Aircraft metrics: http://127.0.0.1:8080/api/aircraft/metrics"