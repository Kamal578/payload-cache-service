import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Response

from payload_cache.api import create_app

REQUEST = '{"list_1":["first string", "ß🙂"],"list_2":["other string", ""]}'
EXPECTED = {"output": "FIRST STRING, OTHER STRING, SS🙂, "}
EXECUTABLE = str(Path(sys.executable).with_name("cache-cli"))


def cli(*args, stdin=None):
    return subprocess.run(
        [EXECUTABLE, *args],
        input=stdin,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
    )


@pytest.fixture
def service(tmp_path, serve):
    calls = []
    requests = []

    def transform(value):
        calls.append(value)
        return value.upper()

    app = create_app(tmp_path / "cache.db", transform)

    @app.middleware("http")
    async def record(request, call_next):
        requests.append(request.method)
        return await call_next(request)

    with serve(app) as host:
        yield host, calls, requests


def test_inline_repeat_and_short_host_alias(service):
    host, calls, requests = service
    result = cli("-H", host, "-j", REQUEST, "-r", "3")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert [json.loads(line) for line in result.stdout.splitlines()] == [EXPECTED] * 3
    assert requests == ["POST", "GET"] * 3
    assert sorted(calls) == sorted(["first string", "other string", "ß🙂", ""])


def test_file_input_and_output(service, tmp_path):
    host, _, _ = service
    source = tmp_path / "input.json"
    target = tmp_path / "output.jsonl"
    source.write_text(REQUEST, encoding="utf-8")
    result = cli("--host", host, "--input", str(source), "--output", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert json.loads(target.read_text(encoding="utf-8")) == EXPECTED


def test_stdin_stdout(service):
    host, _, _ = service
    result = cli("--host", host, "-i", "-", "-o", "-", stdin=REQUEST)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == EXPECTED


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help(flag):
    result = cli(flag)
    assert result.returncode == 0
    assert "--host, -H" in result.stdout
    assert "--repeat, -r" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--unknown"],
        ["-j"],
        ["-j", REQUEST, "-i", "-"],
        ["-j", "{"],
        ["-j", "[]"],
        ["-j", '{"list_1":[1],"list_2":["a"]}'],
        ["-j", '{"list_1":["a"],"list_2":[]}'],
        ["-j", REQUEST, "-r", "0"],
        ["-j", REQUEST, "-r", "-1"],
        ["-j", REQUEST, "-r", "abc"],
        ["-j", REQUEST, "-H", "ftp://example.com"],
        ["-j", REQUEST, "-H", "http://user:pass@example.com"],
        ["-j", REQUEST, "-H", "http://example.com?x=y"],
    ],
)
def test_invalid_arguments(args):
    result = cli(*args)
    assert result.returncode == 2
    assert "invalid input" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_missing_file(tmp_path):
    result = cli("-i", str(tmp_path / "missing.json"))
    assert result.returncode == 3
    assert "No such file" in result.stderr
    assert "Traceback" not in result.stderr


def test_connection_error():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        host = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        result = cli("-H", host, "-j", REQUEST)
    assert result.returncode == 3
    assert "connection failed" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("failure_at", ["post", "get"])
def test_unsuccessful_http_response(serve, failure_at):
    app = FastAPI()

    @app.post("/payload")
    def create():
        return Response(status_code=503) if failure_at == "post" else {"id": "0" * 64}

    @app.get("/payload/{identifier}")
    def read(identifier: str):
        return Response("private server error", status_code=500)

    with serve(app) as host:
        result = cli("-H", host, "-j", REQUEST)
    assert result.returncode == 3
    assert "service returned HTTP" in result.stderr
    assert "private" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("stage", ["create", "read"])
def test_malformed_service_response(serve, stage):
    app = FastAPI()

    @app.post("/payload")
    def create():
        return {"id": "../bad"} if stage == "create" else {"id": "0" * 64}

    @app.get("/payload/{identifier}")
    def read(identifier: str):
        return {"output": 123}

    with serve(app) as host:
        result = cli("-H", host, "-j", REQUEST)
    assert result.returncode == 3
    assert "invalid" in result.stderr
    assert result.stdout == ""


def test_repeat_failure_does_not_overwrite_output(serve, tmp_path):
    app = FastAPI()
    reads = 0

    @app.post("/payload")
    def create():
        return {"id": "0" * 64}

    @app.get("/payload/{identifier}")
    def read(identifier: str):
        nonlocal reads
        reads += 1
        return {"output": "ok"} if reads == 1 else Response(status_code=500)

    target = tmp_path / "existing.jsonl"
    target.write_text("keep me", encoding="utf-8")
    with serve(app) as host:
        result = cli("-H", host, "-j", REQUEST, "-r", "2", "-o", str(target))
    assert result.returncode == 3
    assert result.stdout == ""
    assert target.read_text(encoding="utf-8") == "keep me"


def test_unwritable_output(service, tmp_path):
    host, _, _ = service
    result = cli("-H", host, "-j", REQUEST, "-o", str(tmp_path))
    assert result.returncode == 3
    assert "Traceback" not in result.stderr
