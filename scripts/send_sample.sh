#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
curl --fail-with-body --show-error --silent \
  -X POST http://localhost:5678/webhook/customer-inquiry \
  -H 'Content-Type: application/json' \
  --data-binary @examples/sample_inquiry.json
printf '\nOpen http://localhost:8000/admin to review.\n'
