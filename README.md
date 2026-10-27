# Payload cache service

FastAPI service that uppercases two equally sized lists, interleaves their
results, and persists both individual transformations and generated payloads in
SQLite. A Pydantic Settings CLI exercises the create/read flow over HTTP.

## Setup and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.11 and 3.13 are covered by CI; Docker uses 3.13. Runtime and development
dependencies are separated in `pyproject.toml` and pinned in `uv.lock`.

```sh
git clone https://github.com/Kamal578/payload-cache-service.git
cd payload-cache-service
git switch implement-payload-cache
uv sync --locked
uv run uvicorn payload_cache.api:app --host 127.0.0.1 --port 8000
```

The default database is `data/cache.db`, relative to the working directory.
Set `CACHE_DATABASE` to another SQLite file path if needed. Startup creates its
parent directory and missing tables; shutdown disposes the connection pool.
There are no migrations because this is an initial schema. Existing data must
be migrated deliberately if that schema changes. Interactive API docs: `/docs`.

## API

```sh
curl -sS http://127.0.0.1:8000/payload \
  -H 'Content-Type: application/json' \
  -d '{"list_1":["first string","second string","third string"],"list_2":["other string","another string","last string"]}'
# {"id":"<64 lowercase hexadecimal characters>"}
curl -sS http://127.0.0.1:8000/payload/REPLACE_WITH_ID
# {"output":"FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"}
```

POST `/payload` returns HTTP 200 and `{"id":"..."}` for both new and reused
payloads. GET `/payload/{identifier}` returns `{"output":"..."}`. Elements are
joined with exactly `", "`. Inputs require exactly `list_1` and `list_2`, both
lists of actual strings with equal length. Extra fields and type coercion are
rejected. Empty lists produce `""`; two empty strings produce `", "`. Unicode is
preserved and uppercased using Python's Unicode rules (for example `ß` → `SS`).
Unpaired Unicode surrogates are rejected.

Errors: 422 for invalid input/identifier syntax, 404 for an unknown identifier,
502 for a transformer failure, and 503 for a database failure or lock timeout.
HTTP errors omit internal exception details; diagnostic exceptions go to server
logs. A failed create rolls back its new cache entries and payload together.
Previously committed data survives.

## CLI

```sh
uv run cache-cli -H http://127.0.0.1:8000 \
  -j '{"list_1":["hello"],"list_2":["world"]}' -r 2
# {"output":"HELLO, WORLD"}
# {"output":"HELLO, WORLD"}

# File input; output file is replaced after all iterations succeed.
printf '%s\n' '{"list_1":["hello"],"list_2":["world"]}' > /tmp/cache-request.json
uv run cache-cli --input /tmp/cache-request.json --output /tmp/cache-results.jsonl

# stdin and stdout (the default output).
printf '%s\n' '{"list_1":["hello"],"list_2":["world"]}' | uv run cache-cli -i - -o -
uv run cache-cli --help
```

| Flag | Meaning |
| --- | --- |
| `-H`, `--host URL` | HTTP(S) base URL; default `http://127.0.0.1:8000` |
| `-r`, `--repeat N` | Positive integer; repeat the complete POST then GET, default 1 |
| `-j`, `--json JSON` | Inline request JSON |
| `-i`, `--input FILE` | UTF-8 JSON file; `-` reads stdin |
| `-o`, `--output FILE` | UTF-8 JSON Lines output; `-` writes stdout, the default |
| `-h`, `--help` | Help; host uses `-H` to resolve the specification's alias conflict |

Exactly one of `--input` and `--json` is required. Pydantic Settings performs
argument parsing and validation, with environment/dotenv input disabled for the
CLI. Host URLs may include a base path, but credentials, queries, and fragments
are rejected. Requests have a 30-second timeout and do not follow redirects.

Each successful iteration emits only the GET response as one JSON line. Results
are buffered until every iteration succeeds; a failed HTTP iteration leaves the
output file untouched and stdout empty. An output I/O failure can still leave a
partially written file. Earlier successful API calls remain committed even if a
later iteration or output write fails. Large repeat counts consume memory.

Exit codes: **0** success/help; **2** argument, JSON, or payload validation error;
**3** file/encoding I/O, connection, HTTP, or malformed service-response error.
Errors go to stderr without a traceback. Missing files are I/O errors (3).

## Persistence, identity, and concurrency

- “Payload files” are persisted payload records, served as JSON. Separate files
  would duplicate storage and require coordinating filesystem and DB writes.
- Identity is SHA-256 of compact ASCII-escaped JSON `[list_1, list_2]`. List
  boundaries, order, case, whitespace, and exact Unicode code points matter.
  JSON object property ordering and escape spelling do not matter. Distinct
  inputs that happen to uppercase to the same output keep distinct identifiers.
  SHA-256 has a theoretical collision risk; this is not a security identifier.
- Transformations are cached by the **exact source string**, regardless of its
  list, position, or request. `uppercase` is the replaceable simulated external
  service; `PayloadStore` accepts any callable with the same string-to-string
  contract. Call counts are directly observed in tests.
- `BEGIN IMMEDIATE` takes SQLite's write reservation **before** cache lookup.
  The transaction includes cache misses, transformations, and payload creation.
  Successful concurrent requests sharing the same SQLite file reuse committed
  transformations, even across processes. Writers wait up to 30 seconds; SQLite
  can also briefly block readers during commit. Each operation owns its session.
- This coarse lock serializes all creation, including cache hits and unrelated
  strings. Holding it during an external call is intentional for this small
  assessment; it trades throughput for simple cross-process coordination. Sync
  endpoints execute in FastAPI worker threads, and lifecycle DB work is also
  offloaded. One Uvicorn worker is the documented deployment.
- Tests exercise threads, independent connection pools, and separate OS processes
  against one initialized DB. Simultaneous first-time schema creation by multiple
  workers is not coordinated. Initialize once before starting extra workers.
  Use a local filesystem with reliable SQLite locking, not network storage or
  independent database copies. Distributed deployments need a different design.
- This is **not exactly-once execution of an external service**: calls may repeat
  after failure, rollback, crash, or a lock timeout. No misleading successful
  payload is stored. No retries are hidden in the service.
- Cache semantics assume a deterministic transformer. If its behavior/version
  changes, use a new database or migrate/invalidate both tables together. There
  is no TTL, eviction, authentication, quota, or request-size limit. This is a
  small trusted-use assessment service, not an Internet-facing platform.

## Docker

Requires a running Docker daemon. No Compose or external database is needed.

```sh
docker build -t payload-cache:local .
docker volume create payload-cache-data
docker run --rm --name payload-cache -p 127.0.0.1:8000:8000 \
  --mount type=volume,src=payload-cache-data,dst=/data payload-cache:local
# Stop with Ctrl-C. Run the same command again to reuse the cache and payloads.
```

The image exposes port 8000, runs one worker as UID 10001, and defaults
`CACHE_DATABASE=/data/cache.db`. A fresh named volume inherits the writable
`/data` ownership from the image. For a bind mount or another database path,
ensure UID 10001 can write the directory and database. Keep the volume when
recreating containers; deleting it deletes cached data. Tables initialize at
application startup. Dependencies are installed from the lockfile before source
copying; the runtime environment excludes dev dependencies.

```sh
# Automated smoke check uses its own temporary container and named volume.
sh scripts/smoke-docker.sh payload-cache:local
```

The smoke check exercises a non-root API, the installed CLI, and reading an
existing payload after replacing the container with the same volume.

## Verification

```sh
uv sync --locked
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

CI runs these checks on Python 3.11/3.13 and builds/smoke-tests Docker separately.

| Requirement | Evidence |
| --- | --- |
| Exact sample and interleaving | `test_exact_sample`, `test_interleaving_and_cache_across_requests` |
| Per-string caching and reused identifiers | Recording transformer counters in storage, API restart, CLI repeat tests |
| Stable unambiguous identity | `test_identity_preserves_boundaries_order_case_and_unicode` |
| Persistence across restarts | Storage/API restart tests and Docker volume smoke check |
| Strict validation, missing IDs, empty/Unicode inputs | Parameterized API tests and storage edge cases |
| Concurrent call counts within/across processes | Storage thread, independent-store, spawned-process tests; concurrent API test |
| Failure atomicity and useful errors | Rollback/preserved-cache tests, sanitized API failures, CLI HTTP/protocol failures |
| Actual CLI entry point and every I/O mode | Subprocess tests in `tests/test_cli.py` using a real local HTTP server |
| Repeat, conflicts, bad input/files/network/output | CLI executable tests including failure on a later iteration |
| Reproducible deployment | Lockfile, Dockerfile, CI, `scripts/smoke-docker.sh` |

Record your own English screen-share and camera walkthrough under 15 minutes.
Review the code and report your actual time when submitting.
