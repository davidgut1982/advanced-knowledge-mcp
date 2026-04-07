#!/usr/bin/env python3
"""
Apply knowledge graph migration to database.

This script applies the 003_knowledge_graph.sql migration through
the existing database client.
"""

import os
import sys
from pathlib import Path

# Add project paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from db_client import get_db_client


def apply_migration():
    """Apply the knowledge graph migration."""

    # Set environment - use Supabase since it's configured
    os.environ.setdefault('DB_BACKEND', 'supabase')

    db = get_db_client()
    print("Connected to database")

    # Read migration file
    migration_file = Path(__file__).parent / "migrations" / "003_knowledge_graph.sql"
    with open(migration_file, 'r') as f:
        migration_sql = f.read()

    # Split into individual statements (PostgreSQL/Supabase doesn't support multiple statements in one execute)
    statements = [s.strip() for s in migration_sql.split(';') if s.strip() and not s.strip().startswith('--')]

    print(f"Applying {len(statements)} migration statements...")

    # Apply each statement
    for i, statement in enumerate(statements):
        if statement.strip():
            try:
                print(f"  {i+1}/{len(statements)}: {statement[:50]}...")
                # Use RPC to execute raw SQL
                result = db.rpc('exec_sql', {'sql': statement})
                print(f"    ✓ Success")
            except Exception as e:
                # Try to execute as plain SQL if RPC fails
                print(f"    RPC failed, trying direct SQL: {e}")
                try:
                    # For simple statements, we can try to execute them through table operations
                    if 'CREATE TABLE' in statement.upper():
                        print(f"    CREATE TABLE statement - needs manual application")
                    elif 'CREATE INDEX' in statement.upper():
                        print(f"    CREATE INDEX statement - needs manual application")
                    else:
                        print(f"    Unknown statement type")
                except Exception as e2:
                    print(f"    Failed: {e2}")

    print("Migration application attempt complete")
    print("\nNote: Some statements may need to be applied manually through Supabase dashboard")
    print("SQL Editor -> Run the migration file content")


if __name__ == "__main__":
    apply_migration()