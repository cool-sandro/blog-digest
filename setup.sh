#!/usr/bin/env bash
# Blog Digest – Setup script for Raspberry Pi 5
# Run as your normal user (NOT root), sudo is called where needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CRON_TIME=$(grep -A2 'schedule:' "$SCRIPT_DIR/config.yaml" | grep 'cron:' | awk -F'"' '{print $2}')
CRON_TZ=$(grep -A2 'schedule:' "$SCRIPT_DIR/config.yaml" | grep 'timezone:' | awk -F'"' '{print $2}')
CRON_TIME="${CRON_TIME:-0 7 * * *}"
CRON_TZ="${CRON_TZ:-Europe/Berlin}"

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RESET="\033[0m"

info()  { echo -e "${GREEN}[setup]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[warn]${RESET}  $*"; }

# ── 1. System packages ──────────────────────────────────────────────────────
info "Updating system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv curl git

# ── 2. Python virtualenv ────────────────────────────────────────────────────
info "Creating Python virtualenv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
info "Python dependencies installed."

# ── 3. Ollama ───────────────────────────────────────────────────────────────
if command -v ollama &>/dev/null; then
    info "Ollama already installed: $(ollama --version)"
else
    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

# Start Ollama service (systemd)
if systemctl is-active --quiet ollama 2>/dev/null; then
    info "Ollama service already running."
else
    info "Enabling and starting Ollama service..."
    sudo systemctl enable --now ollama
    sleep 3
fi

# ── 4. Pull model ────────────────────────────────────────────────────────────
MODEL=$(grep -A3 'ollama:' "$SCRIPT_DIR/config.yaml" | grep 'model:' | head -1 | awk '{print $2}' | tr -d '"')
MODEL="${MODEL}"
info "Pulling model: $MODEL  (this may take a few minutes)..."
ollama pull "$MODEL"

# ── 5. Cron job ──────────────────────────────────────────────────────────────
CRON_CMD="$CRON_TIME $VENV_DIR/bin/python $SCRIPT_DIR/digest.py >> $SCRIPT_DIR/digest.log 2>&1"

if crontab -l 2>/dev/null | grep -qF "digest.py"; then
    warn "Cron job already exists – skipping. Edit with: crontab -e"
else
    (crontab -l 2>/dev/null || true; echo "TZ=$CRON_TZ"; echo "$CRON_CMD") | crontab -
    info "Cron job added: $CRON_TIME (TZ=$CRON_TZ)"
fi

# ── 6. Test run ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Setup complete!${RESET}"
echo ""
echo "  Run a test digest now:"
echo "    $VENV_DIR/bin/python $SCRIPT_DIR/digest.py"
echo ""
echo "  Logs: $SCRIPT_DIR/digest.log"
echo "  Output: $SCRIPT_DIR/output/digest-$(date +%Y-%m-%d).html"
