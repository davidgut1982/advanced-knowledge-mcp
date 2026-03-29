#!/bin/bash
# PostgreSQL + pgvector setup script for Knowledge-MCP
set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DB_NAME="${DB_NAME:-knowledge_db}"
DB_USER="${DB_USER:-knowledge_user}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 32)}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

echo_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

echo_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

echo_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

echo_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_requirements() {
    echo_info "Checking system requirements..."

    # Check if PostgreSQL is installed
    if ! command -v psql &> /dev/null; then
        echo_error "PostgreSQL client (psql) is not installed"
        echo_info "Please install PostgreSQL:"
        echo_info "  Ubuntu/Debian: sudo apt install postgresql postgresql-contrib"
        echo_info "  macOS: brew install postgresql"
        echo_info "  CentOS/RHEL: sudo yum install postgresql postgresql-contrib"
        exit 1
    fi

    # Check if PostgreSQL server is running
    if ! pg_isready -h "$DB_HOST" -p "$DB_PORT" &> /dev/null; then
        echo_error "PostgreSQL server is not running on $DB_HOST:$DB_PORT"
        echo_info "Please start PostgreSQL:"
        echo_info "  Ubuntu/Debian: sudo systemctl start postgresql"
        echo_info "  macOS: brew services start postgresql"
        echo_info "  Docker: docker run -d -p 5432:5432 postgres:15"
        exit 1
    fi

    echo_success "System requirements met"
}

install_pgvector() {
    echo_info "Installing pgvector extension..."

    # Check if pgvector is already available
    if sudo -u postgres psql -c "SELECT 1" template1 -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null; then
        echo_success "pgvector extension is already available"
        return 0
    fi

    echo_warning "pgvector extension not found. Attempting to install..."

    # Detect OS and install pgvector
    if [[ -f /etc/debian_version ]]; then
        # Debian/Ubuntu
        echo_info "Detected Debian/Ubuntu system"

        # Try package manager first
        if sudo apt update && sudo apt install -y postgresql-15-pgvector 2>/dev/null; then
            echo_success "Installed pgvector via package manager"
        else
            echo_warning "Package manager installation failed. Building from source..."
            install_pgvector_from_source
        fi

    elif [[ -f /etc/redhat-release ]]; then
        # CentOS/RHEL/Fedora
        echo_info "Detected Red Hat based system"
        install_pgvector_from_source

    elif [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        echo_info "Detected macOS system"
        if command -v brew &> /dev/null; then
            echo_info "Installing pgvector via Homebrew..."
            brew install pgvector
        else
            echo_warning "Homebrew not found. Building from source..."
            install_pgvector_from_source
        fi
    else
        echo_warning "Unknown OS. Attempting to build from source..."
        install_pgvector_from_source
    fi
}

install_pgvector_from_source() {
    echo_info "Building pgvector from source..."

    # Check for build dependencies
    echo_info "Checking build dependencies..."
    if ! command -v make &> /dev/null || ! command -v gcc &> /dev/null; then
        echo_error "Build tools not found. Please install:"
        echo_info "  Ubuntu/Debian: sudo apt install build-essential postgresql-server-dev-15"
        echo_info "  CentOS/RHEL: sudo yum groupinstall 'Development Tools' && sudo yum install postgresql-devel"
        echo_info "  macOS: xcode-select --install"
        exit 1
    fi

    # Create temporary directory
    TEMP_DIR=$(mktemp -d)
    cd "$TEMP_DIR"

    # Clone and build pgvector
    echo_info "Downloading pgvector source..."
    git clone --branch v0.5.1 https://github.com/pgvector/pgvector.git
    cd pgvector

    echo_info "Building pgvector..."
    make

    echo_info "Installing pgvector (requires sudo)..."
    sudo make install

    # Cleanup
    cd /
    rm -rf "$TEMP_DIR"

    echo_success "pgvector built and installed from source"
}

create_database() {
    echo_info "Creating database and user..."

    # Check if database already exists
    if sudo -u postgres psql -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
        echo_warning "Database '$DB_NAME' already exists"
    else
        echo_info "Creating database '$DB_NAME'..."
        sudo -u postgres createdb "$DB_NAME"
        echo_success "Database '$DB_NAME' created"
    fi

    # Check if user already exists
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        echo_warning "User '$DB_USER' already exists"
    else
        echo_info "Creating user '$DB_USER'..."
        sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
        echo_success "User '$DB_USER' created"
    fi

    # Grant privileges
    echo_info "Granting privileges..."
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
    sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL ON SCHEMA public TO $DB_USER;"

    # Create pgvector extension
    echo_info "Creating pgvector extension..."
    sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;"

    echo_success "Database setup completed"
}

run_migrations() {
    echo_info "Running database migrations..."

    local migration_file="../migrations/002_kb_doc_sync.sql"

    if [[ -f "$migration_file" ]]; then
        echo_info "Applying migration: $migration_file"
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$migration_file"
        echo_success "Migration applied successfully"
    else
        echo_warning "Migration file not found: $migration_file"
        echo_info "You may need to run migrations manually after installation"
    fi
}

test_connection() {
    echo_info "Testing database connection..."

    local db_url="postgresql://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME"

    # Test basic connection
    if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null; then
        echo_success "Database connection successful"
    else
        echo_error "Database connection failed"
        exit 1
    fi

    # Test pgvector extension
    if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT extname FROM pg_extension WHERE extname = 'vector';" | grep -q vector; then
        echo_success "pgvector extension available"
    else
        echo_error "pgvector extension not available"
        exit 1
    fi

    # Test vector operations
    if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT '[1,2,3]'::vector;" > /dev/null 2>&1; then
        echo_success "Vector operations working"
    else
        echo_warning "Vector operations may not be working correctly"
    fi
}

create_env_file() {
    echo_info "Creating environment configuration..."

    local env_file="../.env"
    local db_url="postgresql://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME"

    cat > "$env_file" << EOF
# Knowledge-MCP Configuration
# Generated on $(date)

# Database Configuration
DATABASE_URL=$db_url
KNOWLEDGE_DATA_DIR=/opt/knowledge-data

# Optional: Error Monitoring
SENTRY_DSN=
SENTRY_ENVIRONMENT=production

# Optional: Performance Tuning
MAX_CHUNK_SIZE=2000
CONCURRENT_INGESTION_LIMIT=10
EOF

    echo_success "Environment configuration created: $env_file"
    echo_info "Database URL: $db_url"
}

print_summary() {
    echo
    echo_success "PostgreSQL setup completed successfully!"
    echo
    echo_info "Configuration Summary:"
    echo "  Database: $DB_NAME"
    echo "  User: $DB_USER"
    echo "  Password: $DB_PASSWORD"
    echo "  Host: $DB_HOST"
    echo "  Port: $DB_PORT"
    echo "  Connection URL: postgresql://$DB_USER:$DB_PASSWORD@$DB_HOST:$DB_PORT/$DB_NAME"
    echo
    echo_info "Next steps:"
    echo "  1. Install Knowledge-MCP: pip install -e ."
    echo "  2. Test the server: knowledge-mcp"
    echo "  3. Configure Claude Code MCP (see CLAUDE_INTEGRATION.md)"
    echo
    echo_warning "Security Notes:"
    echo "  - Save the database password securely"
    echo "  - Consider using connection pooling for production"
    echo "  - Enable SSL for remote connections"
    echo
}

# Main execution
main() {
    echo_info "Knowledge-MCP PostgreSQL Setup"
    echo_info "==============================="
    echo

    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --db-name)
                DB_NAME="$2"
                shift 2
                ;;
            --db-user)
                DB_USER="$2"
                shift 2
                ;;
            --db-password)
                DB_PASSWORD="$2"
                shift 2
                ;;
            --db-host)
                DB_HOST="$2"
                shift 2
                ;;
            --db-port)
                DB_PORT="$2"
                shift 2
                ;;
            --help)
                echo "Usage: $0 [options]"
                echo "Options:"
                echo "  --db-name NAME      Database name (default: knowledge_db)"
                echo "  --db-user USER      Database user (default: knowledge_user)"
                echo "  --db-password PASS  Database password (default: auto-generated)"
                echo "  --db-host HOST      Database host (default: localhost)"
                echo "  --db-port PORT      Database port (default: 5432)"
                echo "  --help              Show this help message"
                exit 0
                ;;
            *)
                echo_error "Unknown option: $1"
                echo_info "Use --help for usage information"
                exit 1
                ;;
        esac
    done

    # Execute setup steps
    check_requirements
    install_pgvector
    create_database
    run_migrations
    test_connection
    create_env_file
    print_summary
}

# Only run main if script is executed directly (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi