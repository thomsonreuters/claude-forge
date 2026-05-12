#!/usr/bin/env bash
# Start or reuse a Docker container for full QA mode.
#
# Usage:
#   bash start-container.sh                                      # Start or reuse container
#   bash start-container.sh --provider-profile remote-litellm    # Use legacy remote LiteLLM QA profile
#   bash start-container.sh --reset                              # Kill container, remove image, rebuild and start
#   bash start-container.sh --stop                               # Stop and remove container
#   bash start-container.sh --status                             # Check container status
#
# Outputs container name to stdout on success.
# Exit codes: 0=ready, 1=no docker, 2=build failed, 3=start failed

set -euo pipefail

CONTAINER_NAME="forge-qa"
PROVIDER_PROFILE="openrouter"
RESET=false
ACTION="start"

# --- Resolve repo root and image tag ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd -P)"

# Detect Claude Code version from installed binary
if command -v claude &>/dev/null; then
    CLAUDE_VERSION="$(claude --version 2>/dev/null | awk '{print $1}')"
fi
CLAUDE_VERSION="${CLAUDE_VERSION:-latest}"
IMAGE_NAME="forge-claude-test:${CLAUDE_VERSION}"

# --- Helper functions ---
error() { echo "ERROR: $*" >&2; }
info()  { echo "INFO: $*" >&2; }

usage() {
    cat >&2 <<'EOF'
Usage: start-container.sh [--provider-profile openrouter|remote-litellm] [--reset|--stop|--status]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider-profile)
            if [[ -z "${2:-}" ]]; then
                error "--provider-profile requires a value: openrouter or remote-litellm"
                usage
                exit 1
            fi
            PROVIDER_PROFILE="$2"
            shift 2
            ;;
        --provider-profile=*)
            PROVIDER_PROFILE="${1#--provider-profile=}"
            shift
            ;;
        --reset)
            RESET=true
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

case "$PROVIDER_PROFILE" in
    openrouter)
        FORGE_QA_OPENAI_TEMPLATE="openrouter-openai"
        FORGE_QA_GEMINI_TEMPLATE="openrouter-gemini"
        FORGE_QA_ANTHROPIC_TEMPLATE="openrouter-anthropic"
        : "${FORGE_QA_WORKFLOW_MODELS:=deepseek-v4-pro,minimax-m2.7}"
        : "${FORGE_QA_WORKFLOW_MODEL_A:=deepseek-v4-pro}"
        : "${FORGE_QA_WORKFLOW_MODEL_B:=minimax-m2.7}"
        FORGE_QA_DEEPSEEK_TEMPLATE="openrouter-deepseek"
        FORGE_QA_MINIMAX_TEMPLATE="openrouter-minimax"
        ;;
    remote-litellm)
        FORGE_QA_OPENAI_TEMPLATE="litellm-openai"
        FORGE_QA_GEMINI_TEMPLATE="litellm-gemini"
        FORGE_QA_ANTHROPIC_TEMPLATE="litellm-anthropic"
        : "${FORGE_QA_WORKFLOW_MODELS:=gpt-5.5,gemini-3.1-pro-preview}"
        : "${FORGE_QA_WORKFLOW_MODEL_A:=gpt-5.5}"
        : "${FORGE_QA_WORKFLOW_MODEL_B:=gemini-3.1-pro-preview}"
        FORGE_QA_DEEPSEEK_TEMPLATE=""
        FORGE_QA_MINIMAX_TEMPLATE=""
        ;;
    *)
        error "Invalid --provider-profile '$PROVIDER_PROFILE' (expected: openrouter or remote-litellm)"
        exit 1
        ;;
esac

FORGE_QA_PROVIDER_PROFILE="$PROVIDER_PROFILE"
FORGE_QA_OPENAI_PROXY="qa-openai"
FORGE_QA_GEMINI_PROXY="qa-gemini"
FORGE_QA_ANTHROPIC_PROXY="qa-anthropic"

load_env_var() {
    local var="$1"
    if [[ -z "${!var:-}" && -f "$REPO_ROOT/.env" ]]; then
        local val
        val="$(grep "^${var}=" "$REPO_ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
        val="${val%\"}" ; val="${val#\"}"
        val="${val%\'}" ; val="${val#\'}"
        if [[ -n "$val" ]]; then
            printf -v "$var" '%s' "$val"
            export "$var"
        fi
    fi
}

load_qa_env() {
    local var
    for var in \
        GEMINI_API_KEY \
        ANTHROPIC_API_KEY \
        LITELLM_API_KEY \
        LITELLM_BASE_URL \
        OPENAI_API_KEY \
        OPENROUTER_API_KEY \
        OPENROUTER_BASE_URL; do
        load_env_var "$var"
    done
}

validate_provider_profile() {
    load_qa_env

    case "$PROVIDER_PROFILE" in
        openrouter)
            if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
                error "QA provider profile 'openrouter' requires OPENROUTER_API_KEY."
                error "Set it in your environment or repo .env, or use --provider-profile remote-litellm."
                exit 1
            fi
            ;;
        remote-litellm)
            if [[ -z "${LITELLM_API_KEY:-}" || -z "${LITELLM_BASE_URL:-}" ]]; then
                error "QA provider profile 'remote-litellm' requires LITELLM_API_KEY and LITELLM_BASE_URL."
                error "Set both in your environment or repo .env, or use the default OpenRouter profile."
                exit 1
            fi
            ;;
    esac
}

validate_running_container_profile() {
    case "$PROVIDER_PROFILE" in
        openrouter)
            if ! docker exec "$CONTAINER_NAME" sh -c 'test -n "${OPENROUTER_API_KEY:-}"' >/dev/null 2>&1; then
                error "Running QA container for profile 'openrouter' is missing OPENROUTER_API_KEY."
                error "Run 'bash start-container.sh --stop' and restart it with OPENROUTER_API_KEY set."
                exit 3
            fi
            ;;
        remote-litellm)
            if ! docker exec "$CONTAINER_NAME" sh -c \
                'test -n "${LITELLM_API_KEY:-}" && test -n "${LITELLM_BASE_URL:-}"' >/dev/null 2>&1; then
                error "Running QA container for profile 'remote-litellm' is missing LITELLM_API_KEY or LITELLM_BASE_URL."
                error "Run 'bash start-container.sh --stop' and restart it with both variables set."
                exit 3
            fi
            ;;
    esac
}

docker_env_args() {
    local args=(
        -e "PATH=/forge/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        -e "FORGE_HOME=/root/.forge"
        -e "CLAUDE_HOME=/root/.claude"
        -e "FORGE_TEST_REPO=/workspace"
        -e "FORGE_DEBUG=1"
        -e "FORGE_QA_PROVIDER_PROFILE=$FORGE_QA_PROVIDER_PROFILE"
        -e "FORGE_QA_OPENAI_TEMPLATE=$FORGE_QA_OPENAI_TEMPLATE"
        -e "FORGE_QA_GEMINI_TEMPLATE=$FORGE_QA_GEMINI_TEMPLATE"
        -e "FORGE_QA_ANTHROPIC_TEMPLATE=$FORGE_QA_ANTHROPIC_TEMPLATE"
        -e "FORGE_QA_OPENAI_PROXY=$FORGE_QA_OPENAI_PROXY"
        -e "FORGE_QA_GEMINI_PROXY=$FORGE_QA_GEMINI_PROXY"
        -e "FORGE_QA_ANTHROPIC_PROXY=$FORGE_QA_ANTHROPIC_PROXY"
        -e "FORGE_QA_WORKFLOW_MODELS=$FORGE_QA_WORKFLOW_MODELS"
        -e "FORGE_QA_WORKFLOW_MODEL_A=$FORGE_QA_WORKFLOW_MODEL_A"
        -e "FORGE_QA_WORKFLOW_MODEL_B=$FORGE_QA_WORKFLOW_MODEL_B"
        -e "FORGE_QA_DEEPSEEK_TEMPLATE=${FORGE_QA_DEEPSEEK_TEMPLATE:-}"
        -e "FORGE_QA_MINIMAX_TEMPLATE=${FORGE_QA_MINIMAX_TEMPLATE:-}"
    )

    local var
    for var in \
        GEMINI_API_KEY \
        ANTHROPIC_API_KEY \
        LITELLM_API_KEY \
        LITELLM_BASE_URL \
        OPENAI_API_KEY \
        OPENROUTER_API_KEY \
        OPENROUTER_BASE_URL; do
        if [[ -n "${!var:-}" ]]; then
            args+=(-e "$var=${!var}")
        fi
    done

    printf '%s\n' "${args[@]}"
}

# --- Host state dir (mounted into container) ---
HOST_STATE_DIR_RAW="${FORGE_HOME:-$HOME/.forge}/manual-testing/qa"
HOST_STATE_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(os.path.expandvars(sys.argv[1]))))' "$HOST_STATE_DIR_RAW")"
mkdir -p "$HOST_STATE_DIR"

# --- Docker availability check ---
if ! command -v docker &> /dev/null; then
    error "Docker command not found. Install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

if ! docker info &> /dev/null; then
    error "Docker daemon is not running. Start Docker Desktop and try again."
    exit 1
fi

# --- Handle --stop ---
if [[ "$ACTION" == "stop" ]]; then
    if docker ps -q -f "name=^${CONTAINER_NAME}$" | grep -q .; then
        info "Stopping and removing container: $CONTAINER_NAME"
        docker stop "$CONTAINER_NAME" > /dev/null 2>&1 || true
        docker rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
        info "Container removed."
    else
        info "No running container named $CONTAINER_NAME."
        docker rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
    fi
    exit 0
fi

# --- Handle --status ---
if [[ "$ACTION" == "status" ]]; then
    if docker ps -q -f "name=^${CONTAINER_NAME}$" | grep -q .; then
        info "Container $CONTAINER_NAME is running."
        forge_ver="$(docker exec "$CONTAINER_NAME" bash -lc 'cd /forge && uv run python -c "import forge; print(getattr(forge, \"__version__\", \"unknown\"))"' 2>/dev/null || echo "unknown")"
        info "Forge: $forge_ver"
        profile="$(docker exec "$CONTAINER_NAME" sh -c 'printf "%s" "${FORGE_QA_PROVIDER_PROFILE:-unknown}"' 2>/dev/null || echo "unknown")"
        info "QA provider profile: $profile"
        exit 0
    elif docker ps -aq -f "name=^${CONTAINER_NAME}$" | grep -q .; then
        info "Container $CONTAINER_NAME exists but is stopped."
        exit 1
    else
        info "No container named $CONTAINER_NAME."
        exit 1
    fi
fi

# --- Reuse if already running ---
if [[ "$RESET" != "true" ]] && docker ps -q -f "name=^${CONTAINER_NAME}$" | grep -q .; then
    existing_profile="$(docker exec "$CONTAINER_NAME" sh -c 'printf "%s" "${FORGE_QA_PROVIDER_PROFILE:-}"' 2>/dev/null || true)"
    if [[ "$existing_profile" != "$FORGE_QA_PROVIDER_PROFILE" ]]; then
        error "Running container '$CONTAINER_NAME' was created with provider profile '${existing_profile:-unknown}', not '$FORGE_QA_PROVIDER_PROFILE'."
        error "Run 'bash start-container.sh --stop' or rerun QA with --reset before switching provider profiles."
        exit 3
    fi
    for wf_var in FORGE_QA_WORKFLOW_MODELS FORGE_QA_WORKFLOW_MODEL_A FORGE_QA_WORKFLOW_MODEL_B; do
        wf_expected="${!wf_var}"
        wf_actual="$(docker exec "$CONTAINER_NAME" sh -c "printf '%s' \"\${${wf_var}:-}\"" 2>/dev/null || true)"
        if [[ "$wf_actual" != "$wf_expected" ]]; then
            error "Running container '$CONTAINER_NAME' has $wf_var='${wf_actual:-<unset>}', expected '$wf_expected'."
            error "Run 'bash start-container.sh --stop' then restart, or rerun QA with --reset."
            exit 3
        fi
    done
    validate_running_container_profile
    info "Reusing running container: $CONTAINER_NAME"
    echo "$CONTAINER_NAME"
    exit 0
fi

validate_provider_profile

# --- Handle --reset (kill container + remove image, then fall through to rebuild) ---
if [[ "$RESET" == "true" ]]; then
    info "Rebuild: removing container and image..."
    docker stop "$CONTAINER_NAME" > /dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
    docker rmi "$IMAGE_NAME" > /dev/null 2>&1 || true
    info "Cleaned up. Rebuilding from scratch..."
fi

# --- Remove stopped container with same name ---
if docker ps -aq -f "name=^${CONTAINER_NAME}$" | grep -q .; then
    info "Removing stopped container: $CONTAINER_NAME"
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1 || true
fi

DOCKERFILE="$REPO_ROOT/docker/Dockerfile.forge"

# --- Image staleness detection (reuse pattern from scripts/test-integration.sh) ---
get_forge_rev() {
    if command -v git &>/dev/null && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree &>/dev/null; then
        local rev
        rev="$(git -C "$REPO_ROOT" rev-parse HEAD)"
        if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
            echo "${rev}-dirty"
        else
            echo "${rev}"
        fi
        return 0
    fi
    echo "unknown"
}

FORGE_REV="$(get_forge_rev)"

needs_build=false
if ! docker image inspect "$IMAGE_NAME" &> /dev/null; then
    needs_build=true
    info "Image $IMAGE_NAME not found. Building..."
else
    image_rev="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE_NAME")" || {
        info "Failed to read image revision label; forcing rebuild."
        image_rev=""
    }
    if [[ -z "${image_rev}" || "${image_rev}" != "${FORGE_REV}" ]]; then
        needs_build=true
        info "Image stale (image=${image_rev:-<missing>}, repo=${FORGE_REV}). Rebuilding..."
    fi
fi

if [[ "$needs_build" == "true" ]]; then
    if [[ ! -f "$DOCKERFILE" ]]; then
        if docker image inspect "$IMAGE_NAME" &> /dev/null; then
            info "Source repo not available ($DOCKERFILE missing). Using existing image: $IMAGE_NAME"
            needs_build=false
        else
            error "Dockerfile not found at $DOCKERFILE"
            error "Source repo is required to build the QA image."
            error "Fix: run from the Forge source repo or install it so docker/Dockerfile.forge is available."
            exit 2
        fi
    fi

    if [[ "$needs_build" == "true" ]]; then
        info "Building Docker image (this may take a few minutes)..."
        if ! docker build \
            -f "$DOCKERFILE" \
            --build-arg "CLAUDE_VERSION=$CLAUDE_VERSION" \
            --build-arg "FORGE_REV=$FORGE_REV" \
            -t "$IMAGE_NAME" \
            "$REPO_ROOT"; then
            error "Docker build failed."
            exit 2
        fi
        info "Build complete: $IMAGE_NAME"
    fi
fi

# --- Start container ---
info "Starting container: $CONTAINER_NAME"
DOCKER_ENV=()
while IFS= read -r docker_env_arg; do
    DOCKER_ENV+=("$docker_env_arg")
done < <(docker_env_args)
if ! docker run -d \
    --name "$CONTAINER_NAME" \
    "${DOCKER_ENV[@]}" \
    -v "$HOST_STATE_DIR:/workspace/.forge/qa" \
    -w /workspace \
    "$IMAGE_NAME" \
    tail -f /dev/null > /dev/null; then
    error "Failed to start container."
    exit 3
fi

# --- Remove leaked .env before any forge imports ---
# load_dotenv() in cli/main.py:16 fires at import time. If /forge/.env survived
# from a stale image (built before .dockerignore excluded it), it contaminates
# all forge commands. Remove before the "Forge importable" preflight check.
docker exec "$CONTAINER_NAME" bash -c 'rm -f /forge/.env /forge/.env.*'

# --- Preflight inside container ---
info "Running preflight checks..."

# Install jq (many checklist items use it)
docker exec "$CONTAINER_NAME" bash -c 'apt-get update -qq && apt-get install -y -qq jq > /dev/null 2>&1' || {
    error "Failed to install jq in container."
    exit 3
}

# Set a profile for interactive debugging shells. Checklist execution relies on
# docker run -e above so plain docker exec calls see the same values.
{
    echo 'export PATH="/forge/.venv/bin:$PATH"'
    echo 'export FORGE_HOME="/root/.forge"'
    echo 'export CLAUDE_HOME="/root/.claude"'
    echo 'export FORGE_TEST_REPO="/workspace"'
    # QA defaults to debug logging so every Forge command leaves evidence.
    echo 'export FORGE_DEBUG="1"'
    for var in \
        FORGE_QA_PROVIDER_PROFILE \
        FORGE_QA_OPENAI_TEMPLATE \
        FORGE_QA_GEMINI_TEMPLATE \
        FORGE_QA_ANTHROPIC_TEMPLATE \
        FORGE_QA_OPENAI_PROXY \
        FORGE_QA_GEMINI_PROXY \
        FORGE_QA_ANTHROPIC_PROXY \
        FORGE_QA_WORKFLOW_MODELS \
        FORGE_QA_WORKFLOW_MODEL_A \
        FORGE_QA_WORKFLOW_MODEL_B \
        FORGE_QA_DEEPSEEK_TEMPLATE \
        FORGE_QA_MINIMAX_TEMPLATE \
        GEMINI_API_KEY \
        ANTHROPIC_API_KEY \
        LITELLM_API_KEY \
        LITELLM_BASE_URL \
        OPENAI_API_KEY \
        OPENROUTER_API_KEY \
        OPENROUTER_BASE_URL; do
        if [[ -n "${!var:-}" ]]; then
            printf 'export %s=%q\n' "$var" "${!var}"
        fi
    done
} | docker exec -i "$CONTAINER_NAME" bash -c 'cat > /etc/profile.d/forge-qa.sh && chmod 600 /etc/profile.d/forge-qa.sh' || {
    error "Failed to write /etc/profile.d/forge-qa.sh"
    exit 3
}

docker exec "$CONTAINER_NAME" bash -lc 'test -x /forge/.venv/bin/forge' || {
    error "forge not found at /forge/.venv/bin/forge"
    exit 3
}

# Configure Claude Code auth for container environment.
# ANTHROPIC_API_KEY from the env profile (set above) is the sole auth mechanism.
# hasCompletedOnboarding skips the first-run screen.
# settings.json starts empty; `forge extension enable` (section 2) merges hooks into it.
# See: github.com/anthropics/claude-code/issues/9699
docker exec "$CONTAINER_NAME" bash -c 'mkdir -p /root/.claude'

docker exec -i "$CONTAINER_NAME" bash -c 'cat > /root/.claude/settings.json && chmod 600 /root/.claude/settings.json' <<'SETTINGSEOF'
{}
SETTINGSEOF

docker exec -i "$CONTAINER_NAME" bash -c 'cat > /root/.claude.json && chmod 600 /root/.claude.json' <<'ONBOARDEOF'
{"hasCompletedOnboarding":true}
ONBOARDEOF

# Verify Forge is importable
docker exec "$CONTAINER_NAME" bash -lc 'cd /forge && uv run python -c "import forge.cli.main"' || {
    error "Forge is not importable in container."
    exit 3
}

# --- Initialize workspace ---
docker exec "$CONTAINER_NAME" bash -c '
    mkdir -p /workspace/src /workspace/tests /workspace/.claude /workspace/.forge/qa /workspace/.forge/qa/logs
    cd /workspace

    cat > src/main.py << "PYEOF"
def hello():
    return "world"
PYEOF

    cat > tests/test_main.py << "PYEOF"
from src.main import hello

def test_hello():
    assert hello() == "world"
PYEOF

    cat > CLAUDE.md << "PYEOF"
# forge-walkthrough
This is a test repo for the Forge walkthrough skill.
PYEOF

    cat > README.md << "PYEOF"
# forge-walkthrough
Test workspace for the Forge walkthrough skill.
PYEOF

    cat > .claude/settings.local.json << "JSONEOF"
{
  "permissions": {
    "allow": [
      "Bash(npm test)",
      "Bash(uv run pytest*)"
    ]
  },
  "env": {
    "MY_CUSTOM_VAR": "should-survive-forge"
  }
}
JSONEOF

    cat > .gitignore << "GITEOF"
.DS_Store
.idea/
.env
.test-home/
.forge/
__pycache__/
*.pyc
GITEOF

    git init -q -b main
    git config user.email "forge-qa@localhost"
    git config user.name "Forge QA"
    git config commit.gpgsign false
    git add -A
    git commit -q -m "Initial test repo for forge walkthrough --full"
' || {
    error "Failed to initialize workspace in container."
    exit 3
}

info "Container ready: $CONTAINER_NAME (image: $IMAGE_NAME)"
echo "$CONTAINER_NAME"
