import json
import os

from fastapi.testclient import TestClient
from sqlalchemy_utils.functions import (
    create_database,
    database_exists,
    drop_database,
)

from fidesops.core.config import config
from fidesops.db.database import init_db
from fidesops.db.session import get_db_session, get_db_engine
from fidesops.main import app
from fidesops.schemas.jwt import (
    JWE_PAYLOAD_CLIENT_ID,
    JWE_PAYLOAD_SCOPES,
    JWE_ISSUED_AT,
)
from fidesops.api.v1.scope_registry import SCOPE_REGISTRY
from fidesops.util.cache import get_cache
from fidesops.util.oauth_util import generate_jwe
from .fixtures import *
from .integration_fixtures import *

logger = logging.getLogger(__name__)


def migrate_test_db() -> None:
    """Apply migrations at beginning and end of testing session"""
    logger.debug("Applying migrations...")
    assert os.getenv("TESTING", False)
    init_db(config.database.SQLALCHEMY_TEST_DATABASE_URI)
    logger.debug("Migrations successfully applied")


@pytest.fixture(autouse=True, scope="session")
def set_os_env() -> Generator:
    """Sets an environment variable to tell the application it is in test mode."""
    os.environ["TESTING"] = "True"
    yield
    os.environ["TESTING"] = "False"


@pytest.fixture(scope="session")
def db(set_os_env: None) -> Generator:
    """Return a connection to the test DB"""
    # Create the test DB enginge
    assert os.getenv("TESTING", False)
    engine = get_db_engine(
        database_uri=config.database.SQLALCHEMY_TEST_DATABASE_URI,
    )
    logger.debug(f"Configuring database at: {engine.url}")
    if not database_exists(engine.url):
        logger.debug(f"Creating database at: {engine.url}")
        create_database(engine.url)
        logger.debug(f"Database at: {engine.url} successfully created")
    else:
        logger.debug(f"Database at: {engine.url} already exists")

    migrate_test_db()
    SessionLocal = get_db_session(engine=engine)
    the_session = SessionLocal()
    # Setup above...
    yield the_session
    # Teardown below...
    the_session.close()
    engine.dispose()
    logger.debug(f"Dropping database at: {engine.url}")
    # We don't need to perform any extra checks before dropping the DB
    # here since we know the engine will always be connected to the test DB
    drop_database(engine.url)
    logger.debug(f"Database at: {engine.url} successfully dropped")


@pytest.fixture(scope="session")
def cache() -> Generator:
    yield get_cache()


@pytest.fixture(scope="module")
def api_client() -> Generator:
    """Return a client used to make API requests"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def oauth_client(db: Session) -> Generator:
    """Return a client for authentication purposes"""
    client = ClientDetail(
        hashed_secret="thisisatest",
        salt="thisisstillatest",
        scopes=SCOPE_REGISTRY,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    yield client
    client.delete(db)


@pytest.fixture(scope="function")
def generate_auth_header(oauth_client):
    client_id = oauth_client.id

    def _build_jwt(scopes: List[str]):
        payload = {
            JWE_PAYLOAD_SCOPES: scopes,
            JWE_PAYLOAD_CLIENT_ID: client_id,
            JWE_ISSUED_AT: datetime.now().isoformat(),
        }
        jwe = generate_jwe(json.dumps(payload))
        return {"Authorization": "Bearer " + jwe}

    return _build_jwt