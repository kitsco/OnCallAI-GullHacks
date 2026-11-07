#!/usr/bin/env bash
# Builds every image, including BOTH payments-api versions (v1 good, v2 with the bug),
# so the bad-deploy fault can switch versions instantly without building.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE_TAG=v1 docker compose build payments-api fraud-check
IMAGE_TAG=v2 docker compose build payments-api
docker image ls | grep -E "oncall/(payments-api|fraud-check)"
