#!/usr/bin/env bash
# Lanceur automatique tout-en-un pour Linux / macOS
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

find_python() {
    for cmd in python3.12 python3.13 python3.11 python3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            if "$cmd" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "[ERREUR] Python >= 3.11 introuvable. Installez python3.11+ puis relancez."
    exit 1
fi
echo "[1/5] Python OK : $PYTHON_BIN"

if [ ! -x ".venv/bin/python" ]; then
    echo "[2/5] Creation de l'environnement virtuel (.venv)..."
    "$PYTHON_BIN" -m venv .venv
else
    echo "[2/5] Environnement virtuel .venv deja pret."
fi

REQ_HASH_FILE=".venv/.req_hash"
if command -v sha256sum >/dev/null 2>&1; then
    CURRENT_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
else
    CURRENT_HASH="$(shasum -a 256 requirements.txt | awk '{print $1}')"
fi
SAVED_HASH="$(cat "$REQ_HASH_FILE" 2>/dev/null || true)"

if [ "$CURRENT_HASH" != "$SAVED_HASH" ]; then
    echo "[3/5] Installation / mise a jour des dependances Python..."
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
    printf "%s" "$CURRENT_HASH" > "$REQ_HASH_FILE"
else
    echo "[3/5] Dependances Python a jour."
fi

if [ ! -f ".venv/.playwright_ok" ]; then
    echo "[4/5] Installation du navigateur Chromium (Playwright)..."
    .venv/bin/playwright install chromium
    printf "ok" > ".venv/.playwright_ok"
else
    echo "[4/5] Navigateur Chromium deja installe."
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[5/5] Fichier .env cree depuis .env.example."
else
    echo "[5/5] Configuration .env prete."
fi

echo ""
echo "Lancement de l'agent..."
exec .venv/bin/python agent.py "$@"
