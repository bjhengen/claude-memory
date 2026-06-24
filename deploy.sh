#!/bin/bash
# Deploy Claude Memory to a remote host over SSH (rsync + container rebuild).

set -e

# Configuration — provide these via the environment (e.g. an untracked .deploy.env you source).
#   EC2_HOST    SSH target, e.g. ubuntu@your-ec2-host
#   SSH_KEY     path to your private key, e.g. ~/.ssh/your-key.pem
#   REMOTE_DIR  deploy directory on the host (defaults below)
EC2_HOST="${EC2_HOST:?set EC2_HOST, e.g. ubuntu@your-ec2-host}"
SSH_KEY="${SSH_KEY:?set SSH_KEY, e.g. ~/.ssh/your-key.pem}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/claude-memory}"

echo "=== Claude Memory Deployment ==="

# Check if .env exists locally
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in values."
    exit 1
fi

echo "1. Creating remote directory..."
ssh -i $SSH_KEY $EC2_HOST "mkdir -p $REMOTE_DIR"

echo "2. Uploading files..."
scp -i $SSH_KEY -r \
    docker-compose.yml \
    Dockerfile \
    requirements.txt \
    .env \
    src \
    db \
    $EC2_HOST:$REMOTE_DIR/

echo "3. Building and starting containers..."
ssh -i $SSH_KEY $EC2_HOST "cd $REMOTE_DIR && docker-compose down && docker-compose up -d --build"

echo "4. Checking container status..."
ssh -i $SSH_KEY $EC2_HOST "cd $REMOTE_DIR && docker-compose ps"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "The MCP server is running at: http://localhost:8003/mcp (on the host)"
echo ""
echo "To expose via nginx, add the following to your nginx config:"
echo ""
echo "  location /claude-memory/ {"
echo "      proxy_pass http://127.0.0.1:8003/;"
echo "      proxy_http_version 1.1;"
echo "      proxy_set_header Upgrade \$http_upgrade;"
echo "      proxy_set_header Connection 'upgrade';"
echo "      proxy_set_header Host \$host;"
echo "      proxy_set_header X-Real-IP \$remote_addr;"
echo "  }"
echo ""
echo "Then reload nginx: sudo nginx -t && sudo systemctl reload nginx"
