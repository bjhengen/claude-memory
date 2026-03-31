-- V5: Persist OAuth client registrations and tokens to database
-- Fixes: server restarts wiping all OAuth state, breaking client connections

-- OAuth client registrations (long-lived, must survive restarts)
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id VARCHAR(100) PRIMARY KEY,
    client_secret VARCHAR(200),
    client_name VARCHAR(200),
    redirect_uris JSONB DEFAULT '[]',
    grant_types JSONB DEFAULT '[]',
    response_types JSONB DEFAULT '[]',
    token_endpoint_auth_method VARCHAR(50) DEFAULT 'client_secret_post',
    client_id_issued_at BIGINT,
    raw_data JSONB NOT NULL,  -- Full OAuthClientInformationFull serialized
    created_at TIMESTAMP DEFAULT NOW()
);

-- OAuth access tokens
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token VARCHAR(200) PRIMARY KEY,
    client_id VARCHAR(100) REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes JSONB DEFAULT '[]',
    expires_at BIGINT,
    resource VARCHAR(500),
    created_at TIMESTAMP DEFAULT NOW()
);

-- OAuth refresh tokens
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token VARCHAR(200) PRIMARY KEY,
    client_id VARCHAR(100) REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes JSONB DEFAULT '[]',
    expires_at BIGINT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Index for token expiry cleanup
CREATE INDEX IF NOT EXISTS idx_access_tokens_expires ON oauth_access_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires ON oauth_refresh_tokens(expires_at);
