from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from helpers.getenv import get_env


def Check_db_Connection():
    """
    Connects to the Database and returns a session object.

    Returns:
        Session: A SQLAlchemy session object for interacting with the database.
    """
    try:
        engine  = create_engine(get_env("Connection_String", required = True))
        Local_Session = sessionmaker(autocommit = False, autoflush = False, bind = engine)
        if(engine.connect()):
            print("✅ Database Connected Successfully")
            return Local_Session()
    except Exception as e:
        print(f"❌ Failed to Connect to Database: {e}")
        return None
    finally:
        engine.dispose()  # Close the engine connection after use
