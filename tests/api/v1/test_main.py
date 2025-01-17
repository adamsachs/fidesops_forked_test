from starlette.testclient import TestClient

from fidesops.api.v1.urn_registry import V1_URL_PREFIX


def test_read_autogenerated_docs(api_client: TestClient):
    """Test to ensure automatically generated docs build properly"""
    response = api_client.get(f"{V1_URL_PREFIX}/openapi.json")
    assert response.status_code == 200
