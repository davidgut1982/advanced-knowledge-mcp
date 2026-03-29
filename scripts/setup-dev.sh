#!/bin/bash

# Knowledge MCP Development Environment Setup
set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

# Setup development environment
setup_dev_env() {
    print_header "Development Environment Setup"

    # Install UV if not present
    if ! command -v uv &> /dev/null; then
        print_status "Installing UV..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        source $HOME/.cargo/env || true
        export PATH="$HOME/.cargo/bin:$PATH"
    fi

    # Create virtual environment
    print_status "Creating virtual environment..."
    uv venv

    # Activate virtual environment
    source .venv/bin/activate

    # Install package in editable mode with dev dependencies
    print_status "Installing development dependencies..."
    uv pip install -e .
    uv pip install pytest pytest-cov pytest-asyncio mypy ruff black isort pre-commit

    # Install pre-commit hooks
    print_status "Setting up pre-commit hooks..."
    pre-commit install
}

# Setup test database
setup_test_db() {
    print_header "Test Database Setup"

    if command -v docker &> /dev/null; then
        print_status "Starting test PostgreSQL container..."
        docker run -d \
            --name knowledge-test-postgres \
            -e POSTGRES_DB=knowledge_test \
            -e POSTGRES_USER=postgres \
            -e POSTGRES_PASSWORD=test123 \
            -p 5433:5432 \
            pgvector/pgvector:pg15

        # Wait for database to be ready
        sleep 5

        # Create test environment file
        cat > .env.test << EOF
DATABASE_URL=postgresql://postgres:test123@localhost:5433/knowledge_test
KNOWLEDGE_LOG_LEVEL=DEBUG
EOF

        print_status "Test database is running on localhost:5433"
    else
        print_status "Docker not available. Tests will run with JSON backend."
    fi
}

# Run initial tests
run_tests() {
    print_header "Running Tests"

    source .venv/bin/activate

    print_status "Running linting..."
    ruff check src/ tests/ || true

    print_status "Running type checking..."
    mypy src/ --ignore-missing-imports || true

    print_status "Running tests..."
    pytest tests/ -v || true
}

# Print development guidelines
print_dev_info() {
    print_header "Development Guidelines"
    echo ""
    echo "🔧 Available commands:"
    echo "  - Run tests: pytest tests/ -v"
    echo "  - Lint code: ruff check src/ tests/"
    echo "  - Format code: black src/ tests/ && isort src/ tests/"
    echo "  - Type check: mypy src/"
    echo ""
    echo "🗄️  Database:"
    echo "  - Test DB: localhost:5433 (knowledge_test)"
    echo "  - Dev DB: localhost:5432 (knowledge)"
    echo ""
    echo "📁 Project structure:"
    echo "  - src/knowledge_mcp/ - Main source code"
    echo "  - tests/ - Test files"
    echo "  - docs/ - Documentation"
    echo ""
    echo "🔄 Git workflow:"
    echo "  1. Create feature branch: git checkout -b feature/your-feature"
    echo "  2. Make changes and commit"
    echo "  3. Run tests: pytest"
    echo "  4. Push and create PR"
    echo ""
}

# Main setup process
main() {
    print_header "Knowledge MCP Development Setup"

    setup_dev_env
    setup_test_db
    run_tests
    print_dev_info

    print_status "Development environment ready!"
    echo ""
    echo "Activate the environment with: source .venv/bin/activate"
}

# Run main function
main "$@"