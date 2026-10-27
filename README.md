# Payload cache service

A small FastAPI service that uppercases and interleaves two equally sized lists,
with a persistent SQLite cache and a companion CLI. Requires Python 3.11+ and uv.

```sh
uv sync --locked
```

Implementation is developed in incremental, verified commits. API, CLI, tests,
and complete run instructions follow as their milestones are implemented.
