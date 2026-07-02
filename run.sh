#!/usr/bin/env bash
# =============================================================================
# YouTube Soccer Content Agent - Run Script
# =============================================================================
# This script sets up the virtual environment and starts the agent.
# Usage: ./run.sh [options]
#
# Options:
#   --once       Run a single content creation cycle and exit
#   --dry-run    Run in dry-run mode (no actual uploads)
#   --status     Show agent status and exit
#   --queue      Show queued items and exit
#   --history    Show publication history and exit
#   --discover   Run discovery only and show results
#   --dashboard  Launch the web control room (http://127.0.0.1:8787)
#   --auth       Guided YouTube OAuth setup (writes tokens to .env)
#   --doctor     Health check: system, config, YouTube auth, AI, images
#   --help       Show this help message
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ─── Help ──────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--help" ]]; then
    sed -n '/^# Usage:/,/^# =/p' "$0" | sed 's/^# //'
    exit 0
fi

# ─── Check Dependencies ────────────────────────────────────────────────────────

check_deps() {
    local missing=0

    if ! command -v python3 &>/dev/null; then
        log_error "Python 3 is not installed. Install with: apt install python3 python3-pip python3-venv"
        missing=1
    fi

    if ! command -v ffmpeg &>/dev/null; then
        log_warning "ffmpeg is not installed. Install with: apt install ffmpeg"
        log_warning "Video processing will fail without ffmpeg"
    fi

    if ! command -v edge-tts &>/dev/null; then
        log_warning "edge-tts is not installed. Will use pip version."
    fi

    return $missing
}

# ─── Virtual Environment Setup ────────────────────────────────────────────────

setup_venv() {
    if [[ ! -d "venv" ]]; then
        log_info "Creating virtual environment..."
        python3 -m venv venv
        log_success "Virtual environment created"
    fi

    source venv/bin/activate

    if [[ ! -f "venv/.installed" ]]; then
        log_info "Installing dependencies..."
        pip install --quiet --upgrade pip
        pip install --quiet -r requirements.txt
        touch venv/.installed
        log_success "Dependencies installed"
    fi
}

# ─── Logging Setup ─────────────────────────────────────────────────────────────

setup_logging() {
    mkdir -p logs
    local log_file="logs/agent_$(date +%Y%m%d).log"
    echo "==========================================" >> "$log_file"
    echo "Agent started at $(date)" >> "$log_file"
    echo "==========================================" >> "$log_file"
}

# ─── Environment File ──────────────────────────────────────────────────────────

check_env() {
    if [[ ! -f ".env" ]]; then
        log_warning "No .env file found. Creating template..."
        cat > .env << 'ENVEOF'
# YouTube Data API v3 - Required for uploading
# Get from: https://console.cloud.google.com/apis/credentials
YOUTUBE_API_KEY=
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=

# NewsAPI - Optional, for trending soccer news
# Get from: https://newsapi.org/register
NEWSAPI_KEY=

# Pexels - Recommended, for licensed royalty-free background visuals
# Get from: https://www.pexels.com/api/
PEXELS_API_KEY=

# Reddit API - Optional, for scraping soccer subreddits
# Get from: https://www.reddit.com/prefs/apps
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=youtube-soccer-agent/1.0

# OpenAI API - Optional, for AI script generation
# Get from: https://platform.openai.com/api-keys
OPENAI_API_KEY=

# Anthropic API - Optional, alternative AI provider
ANTHROPIC_API_KEY=

# xAI Grok - Optional AI provider (OpenAI-compatible). Tried first if set.
# Get from: https://console.x.ai  | optional: GROK_MODEL=grok-4.3
XAI_API_KEY=

# Local / Ollama Model - Optional, free AI provider (used if no Grok/OpenAI/Anthropic key)
# Local Ollama (default): leave OLLAMA_API_KEY empty.
# Ollama Cloud (direct):  set OLLAMA_API_KEY from ollama.com/settings/keys and use
#   LOCAL_MODEL_ENDPOINT=https://ollama.com/api/generate with a cloud model name.
LOCAL_MODEL_ENDPOINT=http://localhost:11434/api/generate
LOCAL_MODEL_NAME=llama3
OLLAMA_API_KEY=
ENVEOF
        log_warning "Edit .env with your API keys before running"
    fi
}

# ─── Main ──────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║   YouTube Soccer Content Agent              ║${NC}"
    echo -e "${BLUE}║   Autonomous Faceless Channel Creator       ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    check_deps
    check_env
    setup_venv
    setup_logging

    log_info "Starting agent..."
    log_info "Arguments: $*"

    # Guided YouTube OAuth setup (writes tokens into .env)
    if [[ "${1:-}" == "--auth" ]]; then
        log_info "Launching YouTube OAuth setup wizard..."
        python3 setup_oauth.py
        return $?
    fi

    # Health check
    if [[ "${1:-}" == "--doctor" ]]; then
        shift
        python3 doctor.py "$@"
        return $?
    fi

    # Launch the web dashboard instead of the agent loop
    if [[ "${1:-}" == "--dashboard" ]]; then
        shift
        log_info "Launching control room..."
        python3 dashboard.py "$@"
        return $?
    fi

    # Run the agent
    python3 agent.py "$@"

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        log_success "Agent completed successfully"
    else
        log_error "Agent exited with code $exit_code"
    fi

    return $exit_code
}

main "$@"
