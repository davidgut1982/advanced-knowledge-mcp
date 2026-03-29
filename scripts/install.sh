#!/bin/bash

# Knowledge MCP Installation Script
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

# Check if Python is installed
check_python() {
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is required but not installed"
        exit 1
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    print_status "Found Python $PYTHON_VERSION"

    if ! python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)'; then
        print_error "Python 3.8 or higher is required"
        exit 1
    fi
}

# Install UV package manager
install_uv() {
    if ! command -v uv &> /dev/null; then
        print_status "Installing UV package manager..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        source $HOME/.cargo/env || true
        export PATH="$HOME/.cargo/bin:$PATH"
    else
        print_status "UV is already installed"
    fi
}

# Install Knowledge MCP
install_knowledge_mcp() {
    print_status "Installing Knowledge MCP..."

    if [ -f "pyproject.toml" ]; then
        # Installing from source
        print_status "Installing from source..."
        uv venv
        source .venv/bin/activate
        uv pip install -e .
    else
        # Installing from PyPI
        print_status "Installing from PyPI..."
        uv pip install knowledge-mcp
    fi
}

# Setup database (optional)
setup_database() {
    read -p "Do you want to set up a local PostgreSQL database? (y/N): " setup_db

    if [[ $setup_db =~ ^[Yy]$ ]]; then
        print_header "Database Setup"

        if command -v docker &> /dev/null; then
            print_status "Setting up PostgreSQL with Docker..."
            docker run -d \
                --name knowledge-postgres \
                -e POSTGRES_DB=knowledge \
                -e POSTGRES_USER=postgres \
                -e POSTGRES_PASSWORD=knowledge123 \
                -p 5432:5432 \
                pgvector/pgvector:pg15

            print_status "PostgreSQL is running on localhost:5432"
            print_status "Database: knowledge"
            print_status "Username: postgres"
            print_status "Password: knowledge123"
        else
            print_warning "Docker not found. Please install PostgreSQL manually."
            print_status "You can also use Supabase for a hosted database."
        fi
    else
        print_status "Skipping database setup. You can use JSON mode or configure a database later."
    fi
}

# Create configuration file
create_config() {
    print_header "Configuration"

    if [ ! -f ".env" ]; then
        print_status "Creating configuration file..."
        cat > .env << EOF
# Knowledge MCP Configuration

# Database Configuration (choose one)
# Option 1: Local PostgreSQL
DATABASE_URL=postgresql://postgres:knowledge123@localhost:5432/knowledge

# Option 2: Supabase (uncomment and configure)
# SUPABASE_URL=your_supabase_project_url
# SUPABASE_ANON_KEY=your_supabase_anon_key

# Option 3: JSON File Backend (fallback)
# KNOWLEDGE_DATA_DIR=./data

# Logging
KNOWLEDGE_LOG_LEVEL=INFO

# Optional: Sentry error tracking
# SENTRY_DSN=your_sentry_dsn
EOF
        print_status "Created .env configuration file"
        print_warning "Please edit .env to configure your database connection"
    else
        print_status "Configuration file .env already exists"
    fi
}

# Verify installation
verify_installation() {
    print_header "Verification"

    if command -v knowledge-mcp &> /dev/null; then
        print_status "Knowledge MCP installed successfully!"
        knowledge-mcp --help
    else
        print_error "Installation failed"
        exit 1
    fi
}

# Print next steps
print_next_steps() {
    print_header "Next Steps"
    echo ""
    echo "1. Configure your database connection in .env"
    echo "2. Test the installation: knowledge-mcp --help"
    echo "3. Start the server: knowledge-mcp"
    echo "4. See the documentation for Claude Code integration"
    echo ""
    echo "For support, visit: https://github.com/your-org/knowledge-mcp"
    echo ""
}

# Main installation process
main() {
    print_header "Knowledge MCP Installation"

    check_python
    install_uv
    install_knowledge_mcp
    setup_database
    create_config
    verify_installation
    print_next_steps

    print_status "Installation complete!"
}

# Run main function
main "$@"