#!/bin/bash
# Deploy Claude Memory to a remote host over SSH (rsync + compose recreate).
#
# The remote container must stay owned by its compose project. An earlier
# version of this script swapped the mcp container with a plain `docker run`,
# which left it un-owned: the next secrets-manager rollout then failed with
# "container name already in use" AFTER the new secret had been written,
# leaving the parameter store ahead of the running container.
#
# So this script never creates containers itself. It ships code, then hands
# off to the host's deploy hook (a secrets-manager wrapper that exports the
# app's secrets and runs `docker compose up -d --build`), falling back to
# plain compose when no hook is configured.
#
# IMPORTANT: the hook must run as the docker-capable user WITHOUT sudo —
# compose v2 is a per-user CLI plugin and `sudo docker compose` is not found.

set -euo pipefail

# Configuration — provide these via the environment (e.g. an untracked .deploy.env you source).
#   EC2_HOST     SSH target, e.g. ubuntu@your-ec2-host
#   SSH_KEY      path to your private key, e.g. ~/.ssh/your-key.pem
#   REMOTE_DIR   deploy directory on the host (defaults below)
EC2_HOST="${EC2_HOST:?set EC2_HOST, e.g. ubuntu@your-ec2-host}"
SSH_KEY="${SSH_KEY:?set SSH_KEY, e.g. ~/.ssh/your-key.pem}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/claude-memory}"

# Optional deploy hook on the remote host. When set, it is invoked as
# `FRSECRETS_REBUILD=1 <hook> <APP_NAME>` and is expected to export the app's
# secrets from your secrets manager and then run compose. When unset, this
# script runs `docker compose up -d --build` directly (secrets must then come
# from the remote .env).
REMOTE_DEPLOY_HOOK="${REMOTE_DEPLOY_HOOK:-}"
APP_NAME="${APP_NAME:-claude_memory}"

# Variables owned by the secrets manager rather than .env. When a deploy hook
# is configured these are STRIPPED from the .env we ship, so the hook's export
# is the single source of truth on the remote and a stale local copy can never
# shadow it.
SECRET_MANAGED_VARS="${SECRET_MANAGED_VARS:-OPENAI_API_KEY ANTHROPIC_API_KEY}"

MCP_CONTAINER="${MCP_CONTAINER:-claude_memory_mcp}"
ROLLBACK_TAG="${ROLLBACK_TAG:-claude-memory-mcp:rollback}"
HOST_PORT="${HOST_PORT:-8004}"

ssh_run() { ssh -i "$SSH_KEY" "$EC2_HOST" "$@"; }

echo "=== Claude Memory Deployment ==="

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in values."
    exit 1
fi

echo "1. Pre-flight checks..."
ssh_run "mkdir -p $REMOTE_DIR"

# A changed POSTGRES_PASSWORD would rewrite DATABASE_URL while the existing
# database volume still holds the old password — the app would boot and fail
# every query. Compare hashes, never values.
local_pw_hash=$(grep -E '^POSTGRES_PASSWORD=' .env | head -1 | cut -d= -f2- | shasum | cut -c1-12)
remote_pw_hash=$(ssh_run "grep -E '^POSTGRES_PASSWORD=' $REMOTE_DIR/.env 2>/dev/null | head -1 | cut -d= -f2- | shasum | cut -c1-12" || true)
if [ -n "$remote_pw_hash" ] && [ "$local_pw_hash" != "$remote_pw_hash" ]; then
    echo "   ERROR: local POSTGRES_PASSWORD differs from the remote one."
    echo "   Shipping it would break DATABASE_URL against the existing db volume. Aborting."
    exit 1
fi
echo "   POSTGRES_PASSWORD matches remote (or remote has none yet)."

# Tag the currently-running image so a manual rollback stays possible.
ssh_run "if docker inspect $MCP_CONTAINER >/dev/null 2>&1; then \
    docker tag \$(docker inspect $MCP_CONTAINER --format '{{.Image}}') $ROLLBACK_TAG && \
    echo '   Tagged running image as $ROLLBACK_TAG'; \
  else echo '   No running container to tag (first deploy?)'; fi"

echo "2. Uploading files..."
rsync -az --delete --exclude='__pycache__' -e "ssh -i $SSH_KEY" src/ "$EC2_HOST:$REMOTE_DIR/src/"
rsync -az --delete --exclude='__pycache__' -e "ssh -i $SSH_KEY" db/ "$EC2_HOST:$REMOTE_DIR/db/"
rsync -az --exclude='__pycache__' -e "ssh -i $SSH_KEY" scripts/ "$EC2_HOST:$REMOTE_DIR/scripts/"
rsync -az -e "ssh -i $SSH_KEY" docker-compose.yml Dockerfile requirements.txt "$EC2_HOST:$REMOTE_DIR/"

# Ship .env, minus anything the secrets manager owns.
env_to_ship=.env
if [ -n "$REMOTE_DEPLOY_HOOK" ]; then
    env_to_ship=$(mktemp)
    trap 'rm -f "$env_to_ship"' EXIT
    filter=$(printf '%s\n' $SECRET_MANAGED_VARS | paste -sd'|' -)
    grep -vE "^(${filter})=" .env > "$env_to_ship"
    echo "   .env filtered for upload (secrets-manager vars withheld: $SECRET_MANAGED_VARS)"
fi
rsync -az -e "ssh -i $SSH_KEY" "$env_to_ship" "$EC2_HOST:$REMOTE_DIR/.env"

echo "3. Deploying (build happens first; a build failure leaves the old container serving)..."
if [ -n "$REMOTE_DEPLOY_HOOK" ]; then
    echo "   via deploy hook: $REMOTE_DEPLOY_HOOK $APP_NAME"
    ssh_run "FRSECRETS_REBUILD=1 $REMOTE_DEPLOY_HOOK $APP_NAME"
else
    echo "   via plain compose (no deploy hook configured)"
    ssh_run "cd $REMOTE_DIR && docker compose up -d --build"
fi

echo "4. Health check..."
healthy=""
for _ in $(seq 1 15); do
    if ssh_run "curl -sf http://127.0.0.1:${HOST_PORT}/health >/dev/null 2>&1"; then
        healthy=1
        break
    fi
    sleep 2
done

if [ -z "$healthy" ]; then
    echo "   HEALTH CHECK FAILED."
    ssh_run "docker logs $MCP_CONTAINER --tail 30" || true
    echo
    echo "   The old image is tagged $ROLLBACK_TAG. To roll back on the host:"
    echo "     docker tag $ROLLBACK_TAG \$(cd $REMOTE_DIR && docker compose config --images | tail -1)"
    echo "     cd $REMOTE_DIR && docker compose up -d --force-recreate --no-build mcp"
    exit 1
fi
echo "   Healthy."

echo "5. Verifying secrets actually reached the container..."
# Exported != injected: compose only passes variables DECLARED in the service's
# environment: block, and a no-op `up -d` would leave old code running. Read
# PID1's environment rather than trusting the deploy command's exit code.
required_re='(OPENAI_API_KEY|ANTHROPIC_API_KEY|DATABASE_URL|APPROVE_PASSPHRASE|APPROVE_COOKIE_SECRET)'
missing=$(ssh_run "docker exec $MCP_CONTAINER sh -c 'tr \"\\0\" \"\\n\" < /proc/1/environ' \
    | awk -F= '/^${required_re}=/ { if (length(\$2) == 0) print \$1 }
               END {}'" || true)
present=$(ssh_run "docker exec $MCP_CONTAINER sh -c 'tr \"\\0\" \"\\n\" < /proc/1/environ' \
    | grep -cE '^${required_re}=.'" || echo 0)
if [ -n "$missing" ] || [ "$present" -lt 5 ]; then
    echo "   ERROR: required env vars are empty or absent in the container: ${missing:-<not all five present>}"
    echo "   APPROVE_PASSPHRASE/APPROVE_COOKIE_SECRET missing means the /approve gate"
    echo "   fails closed (connector re-auth will not work); the LLM keys gate embedding/judge."
    echo "   Ensure both approve_* params exist under the app's SSM path, then redeploy."
    exit 1
fi
echo "   OPENAI_API_KEY, ANTHROPIC_API_KEY, DATABASE_URL, APPROVE_PASSPHRASE, APPROVE_COOKIE_SECRET all non-empty in PID1."

echo "6. Container state:"
ssh_run "docker ps --filter name=claude_memory --format '   {{.Names}}\t{{.Status}}'"

echo
echo "=== Deployment complete ==="
