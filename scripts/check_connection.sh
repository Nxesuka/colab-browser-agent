#!/usr/bin/env bash
# Test de connectivité PC -> Colab (Ollama via Tailscale) — Linux / macOS
# Usage : ./scripts/check_connection.sh [host] [port] [model]
set -u
HOST="${1:-colab-gpu}"
PORT="${2:-11434}"
MODEL="${3:-qwen3-vl:32b}"
BASE="http://${HOST}:${PORT}"

echo -e "\n[1/5] Statut Tailscale";            tailscale status
echo -e "\n[2/5] ping ${HOST}";                 ping -c 3 "${HOST}"; tailscale ping --c 3 "${HOST}"
echo -e "\n[3/5] ${BASE}/api/tags";             curl -s --max-time 10 "${BASE}/api/tags"; echo
echo -e "\n[4/5] ${BASE}/api/ps";               curl -s --max-time 10 "${BASE}/api/ps"; echo
echo -e "\n[5/5] Génération via /v1/chat/completions"
time curl -s --max-time 300 "${BASE}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"${MODEL}\", \"messages\": [{\"role\": \"user\", \"content\": \"Réponds uniquement : OK\"}], \"max_tokens\": 10}"
echo
