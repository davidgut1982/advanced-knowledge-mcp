# Installation Guide

Complete setup instructions for all Knowledge-MCP backends and deployment options.

## 🚀 Quick Install

### Prerequisites

- **Python 3.11+** (Required)
- **pip** or **uv** (Package manager)
- **PostgreSQL 15+** with pgvector (Recommended)
- **Claude Code** (For MCP integration)

### One-Line Install

```bash
# From PyPI (coming soon)
pip install knowledge-mcp

# From source (current)
pip install git+https://github.com/your-username/knowledge-mcp.git
```

## 🗄️ Backend Setup

### Option 1: PostgreSQL + pgvector (Recommended)

**Best for**: Production, self-hosted, maximum performance

#### Ubuntu/Debian

```bash
# Install PostgreSQL and pgvector
sudo apt update
sudo apt install -y postgresql postgresql-contrib postgresql-15-pgvector

# Create database and user
sudo -u postgres createuser -P knowledge_user
sudo -u postgres createdb -O knowledge_user knowledge_db

# Enable pgvector extension
sudo -u postgres psql knowledge_db -c "CREATE EXTENSION vector;"
```

#### macOS

```bash
# Install via Homebrew
brew install postgresql pgvector

# Start PostgreSQL service
brew services start postgresql

# Create database
createuser knowledge_user
createdb -O knowledge_user knowledge_db

# Enable pgvector
psql knowledge_db -c "CREATE EXTENSION vector;"
```

#### Docker (Recommended)

```bash
# Run PostgreSQL with pgvector
docker run -d \
  --name knowledge-postgres \
  -e POSTGRES_DB=knowledge_db \
  -e POSTGRES_USER=knowledge_user \
  -e POSTGRES_PASSWORD=your_password \
  -p 5432:5432 \
  pgvector/pgvector:pg15

# Test connection
psql -h localhost -U knowledge_user -d knowledge_db -c "SELECT 1;"
```

#### Environment Configuration

```bash
# Create .env file
cat > .env <<EOF
DATABASE_URL=postgresql://knowledge_user:your_password@localhost:5432/knowledge_db
KNOWLEDGE_DATA_DIR=/opt/knowledge-data
SENTRY_DSN=  # Optional
SENTRY_ENVIRONMENT=production
EOF
```

### Option 2: Supabase (Cloud)

**Best for**: Quick start, managed infrastructure, no maintenance

#### Setup Steps

1. **Create Supabase Project**
   ```bash
   # Go to https://supabase.com/dashboard
   # Create new project
   # Note your project URL and service role key
   ```

2. **Run Migrations**
   ```sql
   -- In Supabase SQL Editor, run:

   -- Enable pgvector extension
   CREATE EXTENSION IF NOT EXISTS vector;

   -- Run migration files (see migrations/ directory)
   -- migrations/001_initial_schema.sql
   -- migrations/002_kb_doc_sync.sql
   ```

3. **Configure Environment**
   ```bash
   cat > .env <<EOF
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_KEY=your-service-role-key
   KNOWLEDGE_DATA_DIR=/tmp/knowledge-data  # Fallback only
   EOF
   ```

#### Supabase Configuration

```javascript
// Optional: Custom RLS policies in Supabase dashboard
CREATE POLICY "Allow all operations" ON kb_entries FOR ALL USING (true);
CREATE POLICY "Allow all operations" ON kb_doc_sync FOR ALL USING (true);
CREATE POLICY "Allow all operations" ON research_notes FOR ALL USING (true);
CREATE POLICY "Allow all operations" ON research_sources FOR ALL USING (true);
CREATE POLICY "Allow all operations" ON research_experiments FOR ALL USING (true);
CREATE POLICY "Allow all operations" ON journal_entries FOR ALL USING (true);
```

### Option 3: JSON Files (Development)

**Best for**: Local development, testing, no database setup

#### Auto-Configuration

```bash
# Set data directory (creates automatically)
export KNOWLEDGE_DATA_DIR=/home/user/.knowledge-data

# No other configuration needed!
# JSON files will be created automatically
```

#### Manual Setup

```bash
# Create data directories
mkdir -p ~/.knowledge-data/{kb,research,journal}

# Set permissions
chmod 755 ~/.knowledge-data
chmod 644 ~/.knowledge-data/*

# Configure environment
echo "KNOWLEDGE_DATA_DIR=/home/$USER/.knowledge-data" > .env
```

## 🛠️ Development Setup

### Local Development Environment

```bash
# Clone repository
git clone https://github.com/your-username/knowledge-mcp.git
cd knowledge-mcp

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest tests/

# Start development server
python -m knowledge_mcp.server
```

### Development Dependencies

```bash
# Install with development extras
pip install -e ".[dev,test,docs]"

# Or install manually
pip install pytest pytest-asyncio black flake8 mypy sphinx
```

## 🔧 Claude Code Integration

### MCP Configuration

Add to your Claude Code MCP configuration file:

#### macOS/Linux: `~/.claude/mcp_servers.json`

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "knowledge-mcp",
      "env": {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/knowledge_db"
      }
    }
  }
}
```

#### Windows: `%APPDATA%\.claude\mcp_servers.json`

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "knowledge-mcp.exe",
      "env": {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/knowledge_db"
      }
    }
  }
}
```

#### Alternative: Environment File

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "knowledge-mcp",
      "args": ["--env-file", "/path/to/.env"]
    }
  }
}
```

### Verify Integration

```bash
# Test MCP server directly
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' | knowledge-mcp

# Expected response should include kb_add, kb_search, etc.
```

## 🐳 Docker Deployment

### Single Container

```dockerfile
# Dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install knowledge-mcp
COPY . /app
WORKDIR /app
RUN pip install -e .

# Run server
CMD ["knowledge-mcp"]
```

```bash
# Build and run
docker build -t knowledge-mcp .
docker run -d \
  --name knowledge-mcp \
  -e DATABASE_URL=postgresql://user:pass@host:5432/knowledge_db \
  knowledge-mcp
```

### Docker Compose

```yaml
# docker-compose.yml
version: '3.8'

services:
  postgres:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_DB: knowledge_db
      POSTGRES_USER: knowledge_user
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  knowledge-mcp:
    build: .
    environment:
      DATABASE_URL: postgresql://knowledge_user:${DB_PASSWORD}@postgres:5432/knowledge_db
    depends_on:
      - postgres
    volumes:
      - knowledge_data:/data

volumes:
  postgres_data:
  knowledge_data:
```

```bash
# Deploy with compose
echo "DB_PASSWORD=your_secure_password" > .env
docker-compose up -d
```

## 🗄️ Database Migrations

### Automatic Migration

```bash
# Run on first startup (PostgreSQL/Supabase)
knowledge-mcp --migrate

# Or manually
python -c "
from knowledge_mcp.server import ensure_database_setup
ensure_database_setup()
"
```

### Manual Migration

```bash
# PostgreSQL
psql $DATABASE_URL -f migrations/001_initial_schema.sql
psql $DATABASE_URL -f migrations/002_kb_doc_sync.sql

# Supabase (use SQL Editor in dashboard)
# Copy-paste migration files into Supabase SQL Editor
```

## 🔍 Troubleshooting

### Common Issues

#### Database Connection Failed

```bash
# Check PostgreSQL is running
sudo systemctl status postgresql  # Linux
brew services list | grep postgres  # macOS

# Test connection manually
psql $DATABASE_URL -c "SELECT 1;"

# Check firewall/network
telnet localhost 5432
```

#### Permission Denied

```bash
# Fix data directory permissions
sudo chown -R $USER:$USER ~/.knowledge-data
chmod 755 ~/.knowledge-data

# Fix PostgreSQL permissions
sudo -u postgres psql -c "GRANT ALL ON DATABASE knowledge_db TO knowledge_user;"
```

#### Module Not Found

```bash
# Reinstall in development mode
pip uninstall knowledge-mcp
pip install -e .

# Check Python path
python -c "import knowledge_mcp; print(knowledge_mcp.__file__)"
```

#### MCP Integration Issues

```bash
# Verify Claude Code can find the command
which knowledge-mcp

# Check MCP configuration syntax
python -m json.tool ~/.claude/mcp_servers.json

# Test MCP server directly
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' | knowledge-mcp
```

### Debug Mode

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
knowledge-mcp

# Or in Python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Performance Tuning

#### PostgreSQL Optimization

```sql
-- Increase shared_buffers (25% of RAM)
ALTER SYSTEM SET shared_buffers = '1GB';

-- Optimize for knowledge workload
ALTER SYSTEM SET random_page_cost = 1.1;
ALTER SYSTEM SET effective_cache_size = '3GB';

-- Restart PostgreSQL to apply
```

#### Connection Pooling

```bash
# Install pgbouncer for connection pooling
sudo apt install pgbouncer

# Configure for knowledge-mcp
# DATABASE_URL=postgresql://user:pass@localhost:6432/knowledge_db
```

## 🛡️ Security

### Production Security

```bash
# Use strong passwords
openssl rand -base64 32

# Restrict database access
sudo ufw allow from 10.0.0.0/8 to any port 5432

# Use SSL connections
DATABASE_URL=postgresql://user:pass@host:5432/db?sslmode=require
```

### Environment Variables

```bash
# Never commit .env files
echo ".env" >> .gitignore

# Use secrets management
export DATABASE_URL="$(vault kv get -field=url secret/knowledge-mcp)"
```

## 📊 Monitoring

### Health Checks

```bash
# Basic health check
curl http://localhost:8000/health

# Database connectivity
python -c "
from knowledge_mcp.server import get_db_client
db = get_db_client()
print('Database OK' if db.test_connection() else 'Database FAILED')
"
```

### Logging

```bash
# Configure structured logging
export LOG_FORMAT=json
export LOG_LEVEL=INFO

# Centralized logging
knowledge-mcp 2>&1 | tee /var/log/knowledge-mcp.log
```

## 🚀 Performance Tips

1. **Use PostgreSQL** for production workloads
2. **Enable pgvector** for semantic search performance
3. **Configure connection pooling** for high concurrency
4. **Use SSD storage** for database files
5. **Monitor memory usage** during large document ingestion

## 📞 Support

- **Installation Issues**: [GitHub Issues](https://github.com/your-username/knowledge-mcp/issues)
- **Configuration Help**: [GitHub Discussions](https://github.com/your-username/knowledge-mcp/discussions)
- **Documentation**: [API Reference](API_REFERENCE.md)

---

**Next Steps**: [Claude Code Integration Guide](CLAUDE_INTEGRATION.md)