from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from payload_cache.models import PayloadInput
from payload_cache.storage import PayloadStore, payload_id


class RecordingTransformer:
    def __init__(self):
        self.calls = Counter()
        self.lock = Lock()

    def __call__(self, value):
        with self.lock:
            self.calls[value] += 1
        return value.upper()


@pytest.fixture
def store(tmp_path):
    transformer = RecordingTransformer()
    result = PayloadStore(tmp_path / "cache.db", transformer)
    result.initialize()
    yield result, transformer
    result.close()


def test_interleaving_and_cache_across_requests(store):
    db, transformer = store
    request = PayloadInput(list_1=["a", "b", "a"], list_2=["b", "c", "a"])
    identifier = db.create(request)
    assert db.read(identifier) == "A, B, B, C, A, A"
    assert db.create(request) == identifier
    other = db.create(PayloadInput(list_1=["c"], list_2=["d"]))
    assert db.read(other) == "C, D"
    assert transformer.calls == {"a": 1, "b": 1, "c": 1, "d": 1}


def test_restart_persists_payloads_and_individual_cache(tmp_path):
    path = tmp_path / "cache.db"
    first = PayloadStore(path)
    first.initialize()
    request = PayloadInput(list_1=["hello"], list_2=["world"])
    identifier = first.create(request)
    first.close()

    transformer = RecordingTransformer()
    second = PayloadStore(path, transformer)
    second.initialize()
    try:
        assert second.read(identifier) == "HELLO, WORLD"
        assert second.create(request) == identifier
        assert (
            second.read(second.create(PayloadInput(list_1=["world"], list_2=["hello"])))
            == "WORLD, HELLO"
        )
        assert not transformer.calls
    finally:
        second.close()


def test_identity_preserves_boundaries_order_case_and_unicode():
    examples = [
        (["a,b"], ["c"]),
        (["a"], ["b,c"]),
        (["c"], ["a,b"]),
        (["A,B"], ["c"]),
        (["é"], ["x"]),
        (["e\u0301"], ["x"]),
        ([], []),
        ([""], [""]),
    ]
    assert len(
        {payload_id(PayloadInput(list_1=a, list_2=b)) for a, b in examples}
    ) == len(examples)


@pytest.mark.parametrize(
    "values, expected", [([], ""), ([""], ", "), (["ß🙂"], "SS🙂, SS🙂")]
)
def test_empty_and_unicode(store, values, expected):
    db, transformer = store
    assert db.read(db.create(PayloadInput(list_1=values, list_2=values))) == expected
    assert sum(transformer.calls.values()) == len(set(values))


def test_concurrent_requests_share_cache_across_connections(store):
    db, transformer = store
    requests = [
        PayloadInput(list_1=["shared", str(i % 3)], list_2=["shared", "shared"])
        for i in range(18)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(db.create, requests))
    assert len(set(identifiers)) == 3
    assert transformer.calls == {"shared": 1, "0": 1, "1": 1, "2": 1}
    assert all(db.read(identifier) is not None for identifier in identifiers)


def test_failure_rolls_back_new_cache_and_payload(tmp_path):
    calls = []

    def fails(value):
        calls.append(value)
        if value == "bad":
            raise RuntimeError("external failure")
        return value.upper()

    db = PayloadStore(tmp_path / "cache.db", fails)
    db.initialize()
    request = PayloadInput(list_1=["good"], list_2=["bad"])
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="external failure"):
                db.create(request)
        assert db.read(payload_id(request)) is None
        assert calls == ["good", "bad", "good", "bad"]
    finally:
        db.close()


# A separate engine means an independent connection pool, as in another worker.
def test_independent_stores_share_write_reservation(tmp_path: Path):
    transformer = RecordingTransformer()
    stores = [PayloadStore(tmp_path / "cache.db", transformer) for _ in range(2)]
    for db in stores:
        db.initialize()
    request = PayloadInput(list_1=["shared"], list_2=["shared"])
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(lambda db: db.create(request), stores))
        assert identifiers[0] == identifiers[1]
        assert transformer.calls == {"shared": 1}
    finally:
        for db in stores:
            db.close()


def _process_create(database, call_log, barrier, results, unique):
    import time

    def transform(value):
        with open(call_log, "a", encoding="utf-8") as stream:
            stream.write(value + "\n")
        # Simulate an in-flight external call so competing misses overlap.
        time.sleep(0.02)
        return value.upper()

    db = PayloadStore(Path(database), transform)
    try:
        barrier.wait(timeout=10)
        identifier = db.create(
            PayloadInput(list_1=["shared", unique], list_2=["shared", unique])
        )
        results.put(identifier)
    finally:
        db.close()


def test_separate_processes_do_not_duplicate_transformer_calls(tmp_path):
    import multiprocessing

    database = tmp_path / "cache.db"
    initializer = PayloadStore(database)
    initializer.initialize()
    initializer.close()
    call_log = tmp_path / "calls.txt"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_create,
            args=(str(database), str(call_log), barrier, results, unique),
        )
        for unique in ("left", "right")
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        identifiers = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert len(set(identifiers)) == 2
        assert Counter(call_log.read_text(encoding="utf-8").splitlines()) == {
            "shared": 1,
            "left": 1,
            "right": 1,
        }
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()


def test_failed_request_preserves_previously_committed_cache(tmp_path):
    calls = []

    def transform(value):
        calls.append(value)
        if value == "bad":
            raise RuntimeError("unavailable")
        return value.upper()

    db = PayloadStore(tmp_path / "cache.db", transform)
    db.initialize()
    try:
        identifier = db.create(PayloadInput(list_1=["good"], list_2=["good"]))
        with pytest.raises(RuntimeError):
            db.create(PayloadInput(list_1=["good"], list_2=["bad"]))
        assert db.read(identifier) == "GOOD, GOOD"
        db.create(PayloadInput(list_1=["good", "good"], list_2=["good", "good"]))
        assert calls == ["good", "bad"]
    finally:
        db.close()


def test_invalid_transformer_result_is_not_cached(tmp_path):
    db = PayloadStore(tmp_path / "cache.db", lambda value: None)
    db.initialize()
    request = PayloadInput(list_1=["a"], list_2=["a"])
    try:
        with pytest.raises(TypeError, match="transformer must return a string"):
            db.create(request)
        assert db.read(payload_id(request)) is None
    finally:
        db.close()
