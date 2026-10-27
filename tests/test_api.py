from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from payload_cache.api import create_app
from payload_cache.models import PayloadInput
from payload_cache.storage import payload_id


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "cache.db")) as client:
        yield client


def test_exact_sample(client):
    request = {
        "list_1": ["first string", "second string", "third string"],
        "list_2": ["other string", "another string", "last string"],
    }
    response = client.post("/payload", json=request)
    assert response.status_code == 200
    identifier = response.json()["id"]
    assert response.json() == {"id": identifier}
    assert len(identifier) == 64
    output = client.get(f"/payload/{identifier}")
    assert output.status_code == 200
    assert output.json() == {
        "output": "FIRST STRING, OTHER STRING, SECOND STRING, "
        "ANOTHER STRING, THIRD STRING, LAST STRING"
    }
    assert client.post("/payload", json=request).json() == response.json()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"list_1": ["a"], "list_2": []},
        {"list_1": [1], "list_2": ["a"]},
        {"list_1": [True], "list_2": ["a"]},
        {"list_1": [None], "list_2": ["a"]},
        {"list_1": "a", "list_2": ["a"]},
        {"list_1": [], "list_2": [], "extra": "x"},
        {"list_1": None, "list_2": []},
        {"list_1": ["\ud800"], "list_2": ["a"]},
    ],
)
def test_invalid_inputs(client, body):
    # ensure_ascii also allows testing an invalid escaped Unicode surrogate.
    import json

    response = client.post(
        "/payload",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_malformed_json(client):
    assert client.post("/payload", content="{").status_code == 422


def test_missing_identifier(client):
    assert client.get("/payload/" + "0" * 64).status_code == 404
    assert client.get("/payload/not-an-id").status_code == 422


@pytest.mark.parametrize(
    "values, expected", [([], ""), ([""], ", "), (["ß🙂"], "SS🙂, SS🙂")]
)
def test_edge_outputs(client, values, expected):
    identifier = client.post(
        "/payload", json={"list_1": values, "list_2": values}
    ).json()["id"]
    assert client.get(f"/payload/{identifier}").json() == {"output": expected}


def test_api_restart_reuses_cache(tmp_path):
    path = tmp_path / "cache.db"
    calls = Counter()

    def transform(value):
        calls[value] += 1
        return value.upper()

    request = {"list_1": ["a"], "list_2": ["b"]}
    with TestClient(create_app(path, transform)) as first:
        identifier = first.post("/payload", json=request).json()["id"]
    with TestClient(create_app(path, transform)) as second:
        assert second.get(f"/payload/{identifier}").json() == {"output": "A, B"}
        assert second.post("/payload", json=request).json() == {"id": identifier}
        second.post("/payload", json={"list_1": ["b"], "list_2": ["a"]})
    assert calls == {"a": 1, "b": 1}


def test_concurrent_api_requests(tmp_path):
    calls = Counter()

    def transform(value):
        calls[value] += 1
        return value.upper()

    with TestClient(create_app(tmp_path / "cache.db", transform)) as client:

        def create(i):
            response = client.post(
                "/payload", json={"list_1": ["a"], "list_2": [str(i % 2)]}
            )
            assert response.status_code == 200
            return response.json()["id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            identifiers = list(pool.map(create, range(12)))
        assert len(set(identifiers)) == 2
        assert calls == {"a": 1, "0": 1, "1": 1}


def test_transformer_failure_is_sanitized_and_rolls_back(tmp_path):
    def fail(value):
        raise RuntimeError("private upstream credentials")

    request = {"list_1": ["a"], "list_2": ["b"]}
    with TestClient(create_app(tmp_path / "cache.db", fail)) as client:
        response = client.post("/payload", json=request)
        assert response.status_code == 502
        assert response.json() == {"detail": "Payload transformation failed"}
        identifier = payload_id(PayloadInput(**request))
        assert client.get(f"/payload/{identifier}").status_code == 404


def test_database_failure_is_sanitized(tmp_path, monkeypatch):
    def fail(*args):
        raise OperationalError("private SQL", {}, RuntimeError("private path"))

    with TestClient(create_app(tmp_path / "cache.db")) as client:
        monkeypatch.setattr("payload_cache.storage.PayloadStore.create", fail)
        monkeypatch.setattr("payload_cache.storage.PayloadStore.read", fail)
        for response in (
            client.post("/payload", json={"list_1": [], "list_2": []}),
            client.get("/payload/" + "0" * 64),
        ):
            assert response.status_code == 503
            assert response.json() == {
                "detail": "Payload storage is temporarily unavailable"
            }
