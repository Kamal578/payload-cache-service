#!/bin/sh
# Build first; this checks HTTP, CLI availability, non-root writes, and restart persistence.
set -eu
image=${1:-payload-cache:local}
name="payload-cache-smoke-$$"
volume="payload-cache-smoke-data-$$"
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "$volume" >/dev/null
start() {
    docker run -d --name "$name" --mount "type=volume,src=$volume,dst=/data" "$image" >/dev/null
    attempt=0
    until docker exec "$name" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=1)' >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            docker logs "$name"
            exit 1
        fi
        sleep 1
    done
}
start
docker exec "$name" python -c 'import os; assert os.getuid() != 0'
identifier=$(docker exec "$name" python -c '
import json, urllib.request
body = json.dumps({"list_1": ["first string", "second string", "third string"], "list_2": ["other string", "another string", "last string"]}).encode()
request = urllib.request.Request("http://127.0.0.1:8000/payload", data=body, headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(request))["id"])
')
docker rm -f "$name" >/dev/null
start
docker exec "$name" python -c '
import json, sys, urllib.request
output = json.load(urllib.request.urlopen("http://127.0.0.1:8000/payload/" + sys.argv[1]))
assert output == {"output": "FIRST STRING, OTHER STRING, SECOND STRING, ANOTHER STRING, THIRD STRING, LAST STRING"}, output
' "$identifier"
docker exec "$name" cache-cli --host http://127.0.0.1:8000 --json '{"list_1":["other string"],"list_2":["first string"]}' --repeat 2
printf '%s\n' 'Docker smoke test passed: non-root API, CLI, and volume persistence.'
