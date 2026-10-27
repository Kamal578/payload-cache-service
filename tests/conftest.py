import socket
import time
from contextlib import contextmanager
from threading import Thread

import pytest
import uvicorn


@pytest.fixture
def serve():
    @contextmanager
    def running(app):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="critical", access_log=False)
            )
            thread = Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        pytest.fail("test HTTP server failed to start")
                    time.sleep(0.01)
                yield f"http://127.0.0.1:{port}"
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                assert not thread.is_alive(), "test HTTP server failed to stop"

    return running
