import os
import sys
from pathlib import Path

# Add project paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from db_client import get_db_client

# Set environment for local PostgreSQL
os.environ['DB_BACKEND'] = 'local'
os.environ['DB_HOST'] = 'localhost'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'mpm_system'
os.environ['DB_USER'] = 'latvian_user'
os.environ['DB_PASSWORD'] = 'latvian_dev_password_2026'

try:
    db = get_db_client()
    print("✓ Connected to local PostgreSQL database")
    
    # Check if tables exist
    try:
        result = db.table('knowledge.kg_nodes').select('*').limit(1).execute()
        print("✓ knowledge.kg_nodes table exists")
    except Exception as e:
        print(f"✗ knowledge.kg_nodes table does not exist: {e}")

    try:
        result = db.table('knowledge.kg_edges').select('*').limit(1).execute()
        print("✓ knowledge.kg_edges table exists")
    except Exception as e:
        print(f"✗ knowledge.kg_edges table does not exist: {e}")

    # Check data counts
    try:
        result = db.table('knowledge.kg_nodes').select('*', count='exact').execute()
        print(f"✓ kg_nodes count: {result.count}")
    except Exception as e:
        print(f"✗ Failed to count kg_nodes: {e}")

    try:
        result = db.table('knowledge.kb_entries').select('*', count='exact').execute()
        print(f"✓ kb_entries count: {result.count}")
    except Exception as e:
        print(f"✗ Failed to count kb_entries: {e}")
        
except Exception as e:
    print(f"✗ Failed to connect: {e}")
