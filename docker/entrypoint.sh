#!/bin/bash

# Knowledge MCP Docker Entrypoint Script
set -e

# Function to log messages
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Wait for PostgreSQL if DATABASE_URL is provided
if [ -n "$DATABASE_URL" ] || [ -n "$SUPABASE_URL" ]; then
    log "Database configuration detected, checking connectivity..."

    # Extract host and port for health check
    if [ -n "$DATABASE_URL" ]; then
        DB_HOST=$(echo "$DATABASE_URL" | sed -n 's/.*@\([^:]*\):.*/\1/p')
        DB_PORT=$(echo "$DATABASE_URL" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')

        if [ -n "$DB_HOST" ] && [ -n "$DB_PORT" ]; then
            log "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
            timeout 60s bash -c "until nc -z $DB_HOST $DB_PORT; do sleep 1; done"
            log "PostgreSQL is available"
        fi
    fi
fi

# Set up data directory structure
log "Setting up data directories..."
mkdir -p "$KNOWLEDGE_DATA_DIR"
mkdir -p "$(dirname "$KNOWLEDGE_DATA_DIR")/logs"

# Run database migrations if needed
if [ "$1" = "knowledge-mcp" ] && [ -n "$DATABASE_URL" ]; then
    log "Running database setup..."
    # Add migration command here if available
    # python -m knowledge_mcp.migrations || true
fi

# Log startup information
log "Starting Knowledge MCP Server..."
log "Data directory: $KNOWLEDGE_DATA_DIR"
log "Log level: ${KNOWLEDGE_LOG_LEVEL:-INFO}"

# Execute the main command
exec "$@"