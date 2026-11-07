#!/usr/bin/env bash
# Sends 10 test charges through nginx and prints the results. Usage: ./scripts/smoke_test.sh [host]
HOST="${1:-http://localhost}"
for i in $(seq 1 10); do
  cents=$(( RANDOM % 49500 + 500 ))
  amount=$(printf "%d.%02d" $((cents / 100)) $((cents % 100)))
  curl -s -X POST "$HOST/charges" \
    -H "Content-Type: application/json" \
    -d "{\"amount\": $amount, \"currency\": \"USD\", \"card_token\": \"tok_test_$RANDOM\", \"merchant_id\": \"m$((RANDOM % 5 + 1))\"}"
  echo
done
