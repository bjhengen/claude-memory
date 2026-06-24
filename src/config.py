"""Configuration from environment variables."""

import os

from mcp.server.transport_security import TransportSecuritySettings

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://claude:claude@localhost:5432/claude_memory")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Public host the server is served under (behind a TLS reverse proxy in production).
# Set PUBLIC_HOST in the deployment environment, e.g. "memory.example.com".
PUBLIC_HOST = os.getenv("PUBLIC_HOST")
ISSUER_URL = os.getenv(
    "OAUTH_ISSUER_URL",
    f"https://{PUBLIC_HOST}" if PUBLIC_HOST else "http://localhost:8003",
)

# Configure transport security (DNS-rebinding protection) to allow external access.
# Localhost is always allowed; the public host is added when PUBLIC_HOST is set.
_allowed_hosts = ["localhost:8003", "127.0.0.1:8003"]
if PUBLIC_HOST:
    _allowed_hosts += [PUBLIC_HOST, f"{PUBLIC_HOST}:80", f"{PUBLIC_HOST}:443", f"{PUBLIC_HOST}:*"]

security_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
)
