import os
import sys
from sqlalchemy import inspect
import subprocess

# Add src to python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from helpers.get_env import load_environment_variables
load_environment_variables()

from config.database import get_engine
from models.auth_model import Base
from models import file_model  # noqa: F401

def bootstrap():
    engine = get_engine()
    inspector = inspect(engine)
    
    # Check if there are any tables in the database
    tables = inspector.get_table_names()
    
    if not tables:
        print("Empty database detected. Creating all tables from SQLAlchemy models...")
        Base.metadata.create_all(bind=engine)
        print("✅ Tables created successfully.")
        
        print("Stamping database with Alembic head revision...")
        result = subprocess.run(["python", "-m", "alembic", "stamp", "head"], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Database successfully stamped to head.")
        else:
            print("❌ Failed to stamp database:")
            print(result.stderr)
            sys.exit(1)
    else:
        print("Existing database tables detected. Running Alembic migrations to upgrade to head...")
        result = subprocess.run(["python", "-m", "alembic", "upgrade", "head"], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Alembic migrations completed successfully.")
        else:
            print("❌ Alembic migrations failed:")
            print(result.stderr)
            sys.exit(1)

if __name__ == "__main__":
    bootstrap()
