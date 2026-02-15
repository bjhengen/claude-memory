"""Configuration from environment variables."""

import os

from mcp.server.transport_security import TransportSecuritySettings

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://claude:claude@localhost:5432/claude_memory")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
API_KEY = os.getenv("CLAUDE_MEMORY_API_KEY", "dev-key")  # For backward-compatible API key auth
ISSUER_URL = os.getenv("OAUTH_ISSUER_URL", "https://memory.friendly-robots.com")

# Configure transport security to allow external access
security_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "localhost:8003",
        "127.0.0.1:8003",
        "memory.friendly-robots.com",
        "memory.friendly-robots.com:80",
        "memory.friendly-robots.com:443",
        "memory.friendly-robots.com:*",
    ]
)
