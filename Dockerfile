# Claude Memory MCP Server
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/ ./src/
COPY db/ ./db/

# Run the MCP server with uvicorn
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8003"]
