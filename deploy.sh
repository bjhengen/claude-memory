#!/bin/bash
# Deploy Claude Memory to a remote host over SSH (rsync + safe container swap).
#
# The remote docker-compose (v1) recreate path is broken against newer Docker
# engines (ContainerConfig KeyError) and takes the db container down with it.
# This script therefore never runs compose on the remote: it builds the new
# image first, then swaps ONLY the mcp container with plain `docker run`,
# health-checks it, and rolls back to the previous container on failure.
# The db container is never touched.

set -euo pipefail

# Configuration — provide these via the environment (e.g. an untracked .deploy.env you source).
#   EC2_HOST    SSH target, e.g. ubuntu@your-ec2-host
#   SSH_KEY     path to your private key, e.g. ~/.ssh/your-key.pem
#   REMOTE_DIR  deploy directory on the host (defaults below)
EC2_HOST="${EC2_HOST:?set EC2_HOST, e.g. ubuntu@your-ec2-host}"
SSH_KEY="${SSH_KEY:?set SSH_KEY, e.g. ~/.ssh/your-key.pem}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/claude-memory}"

# Container topology — matches docker-compose.yml (compose prefixes the
# network with its project name, i.e. the remote directory basename).
MCP_CONTAINER="${MCP_CONTAINER:-claude_memory_mcp}"
IMAGE_TAG="${IMAGE_TAG:-claude-memory-mcp}"
DOCKER_NETWORK="${DOCKER_NETWORK:-claude-memory_claude_memory_net}"
HOST_PORT="${HOST_PORT:-8004}"

echo "=== Claude Memory Deployment ==="

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in values."
    exit 1
fi

echo "1. Creating remote directory..."
ssh -i "$SSH_KEY" "$EC2_HOST" "mkdir -p $REMOTE_DIR"

echo "2. Uploading files..."
rsync -az --delete --exclude='__pycache__' -e "ssh -i $SSH_KEY" src/ "$EC2_HOST:$REMOTE_DIR/src/"
rsync -az --delete --exclude='__pycache__' -e "ssh -i $SSH_KEY" db/ "$EC2_HOST:$REMOTE_DIR/db/"
rsync -az --exclude='__pycache__' -e "ssh -i $SSH_KEY" scripts/ "$EC2_HOST:$REMOTE_DIR/scripts/"
rsync -az -e "ssh -i $SSH_KEY" docker-compose.yml Dockerfile requirements.txt .env "$EC2_HOST:$REMOTE_DIR/"

echo "3. Building image on remote (old container keeps serving during build)..."
ssh -i "$SSH_KEY" "$EC2_HOST" "docker build -t $IMAGE_TAG $REMOTE_DIR"

echo "4. Swapping mcp container (db is never touched)..."
ssh -i "$SSH_KEY" "$EC2_HOST" \
    "MCP_CONTAINER=$MCP_CONTAINER IMAGE_TAG=$IMAGE_TAG DOCKER_NETWORK=$DOCKER_NETWORK HOST_PORT=$HOST_PORT REMOTE_DIR=$REMOTE_DIR bash -s" <<'REMOTE'
set -eo pipefail
cd "$REMOTE_DIR"

old_id=$(docker ps -aq -f name="^${MCP_CONTAINER}$")

# Runtime-only secrets (e.g. SSM-injected at provision time) live in the
# running container, not in .env — carry them across the swap. Explicit -e
# flags override --env-file values.
EXTRA_ENV=()
if [ -n "$old_id" ]; then
    for var in DATABASE_URL OPENAI_API_KEY ANTHROPIC_API_KEY; do
        val=$(docker exec "$MCP_CONTAINER" printenv "$var" 2>/dev/null || true)
        if [ -n "$val" ]; then
            EXTRA_ENV+=("-e" "$var=$val")
        fi
    done
fi

# Refuse to swap into a container with no LLM keys: a silent empty fetch here
# would boot a server that fails on every embedding/judge call.
for required in OPENAI_API_KEY ANTHROPIC_API_KEY; do
    if ! printf '%s\n' "${EXTRA_ENV[@]}" | grep -q "^${required}=" \
        && ! grep -q "^${required}=." .env; then
        echo "ERROR: $required found neither in the running container nor in .env — aborting before swap."
        exit 1
    fi
done

if [ -n "$old_id" ]; then
    docker rename "$MCP_CONTAINER" "${MCP_CONTAINER}_old"
    docker stop "${MCP_CONTAINER}_old" >/dev/null
fi

docker run -d --name "$MCP_CONTAINER" \
    --network "$DOCKER_NETWORK" \
    -p "127.0.0.1:${HOST_PORT}:8003" \
    --restart unless-stopped \
    --env-file .env \
    "${EXTRA_ENV[@]}" \
    "$IMAGE_TAG" >/dev/null

echo "   Waiting for health check..."
healthy=""
for _ in $(seq 1 15); do
    if curl -sf "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 2
done

if [ -z "$healthy" ]; then
    echo "   Health check FAILED — rolling back to previous container."
    docker logs "$MCP_CONTAINER" --tail 30 || true
    docker stop "$MCP_CONTAINER" >/dev/null || true
    docker rm "$MCP_CONTAINER" >/dev/null || true
    if [ -n "$old_id" ]; then
        docker rename "${MCP_CONTAINER}_old" "$MCP_CONTAINER"
        docker start "$MCP_CONTAINER" >/dev/null
        echo "   Previous container restored."
    fi
    exit 1
fi

if [ -n "$old_id" ]; then
    docker rm "${MCP_CONTAINER}_old" >/dev/null
fi
echo "   Healthy."
REMOTE

echo "5. Container status..."
ssh -i "$SSH_KEY" "$EC2_HOST" "docker ps --filter name=claude_memory --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "The MCP server is running at: http://localhost:${HOST_PORT}/mcp (on the host)"
echo ""
echo "To expose via nginx, add the following to your nginx config:"
echo ""
echo "  location /claude-memory/ {"
echo "      proxy_pass http://127.0.0.1:${HOST_PORT}/;"
echo "      proxy_http_version 1.1;"
echo "      proxy_set_header Upgrade \$http_upgrade;"
echo "      proxy_set_header Connection 'upgrade';"
echo "      proxy_set_header Host \$host;"
echo "      proxy_set_header X-Real-IP \$remote_addr;"
echo "  }"
echo ""
echo "Then reload nginx: sudo nginx -t && sudo systemctl reload nginx"
