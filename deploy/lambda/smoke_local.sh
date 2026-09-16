#!/usr/bin/env bash
# Smoke-test the Lambda container image locally with the Lambda Runtime Interface Emulator
# that the public.ecr.aws/lambda base image ships: start the container, POST API Gateway
# HTTP API v2.0 events to the RIE invocation endpoint, print the responses and assert on them.
#
#   IMAGE=slawatch-lambda:local PORT=9000 deploy/lambda/smoke_local.sh
#
# Needs docker, curl and python3. No AWS credentials, no network calls beyond localhost.
set -euo pipefail

IMAGE="${IMAGE:-slawatch-lambda:local}"
PORT="${PORT:-9000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVENTS="${HERE}/events"
NAME="slawatch-rie-$$"
URL="http://localhost:${PORT}/2015-03-31/functions/function/invocations"

cleanup() {
  if [ -n "${KEEP_LOGS:-}" ] || [ "${status:-1}" -ne 0 ]; then
    echo "--- container logs (${NAME}) ---" >&2
    docker logs "${NAME}" >&2 2>&1 || true
  fi
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
status=1

echo "==> starting ${IMAGE} as ${NAME} on :${PORT} (Lambda Runtime Interface Emulator)"
docker run -d --name "${NAME}" -p "${PORT}:8080" "${IMAGE}" >/dev/null

# The emulator listens as soon as the container is up; the first invocation triggers
# function init (model load), so it is the slow one.
invoke() {  # invoke <event file> -> prints the raw Lambda response
  curl -sS --retry 30 --retry-all-errors --retry-delay 1 --max-time 120 \
    -H 'content-type: application/json' -X POST "${URL}" --data-binary "@$1"
}

check() {  # check <name> <lambda response json> <python assertion body>
  python3 - "$1" "$2" <<PY
import json, sys
name, raw = sys.argv[1], sys.argv[2]
resp = json.loads(raw)
body = json.loads(resp.get("body") or "null")
print(f"[{name}] statusCode={resp.get('statusCode')} body={json.dumps(body)}")
$3
print(f"[{name}] ok")
PY
}

t0=$(python3 -c "import time; print(int(time.time() * 1000))")
r=$(invoke "${EVENTS}/health.json")
t1=$(python3 -c "import time; print(int(time.time() * 1000))")
echo "==> first invocation (container start + init incl. model load): $((t1 - t0)) ms"
check health "$r" '
assert resp["statusCode"] == 200, resp
assert body["status"] == "ok", body
assert body["model_version"], body
assert body["synthetic_data"] is True, body
assert body["training_window"]["first_month"], body
'

r=$(invoke "${EVENTS}/score.json")
check score "$r" '
assert resp["statusCode"] == 200, resp
assert 0.0 <= body["probability"] <= 1.0, body
assert body["risk_band"] in ("low", "medium", "high"), body
assert body["model_version"], body
assert 0.0 < body["threshold"] < 1.0, body
# golden value for the documented example ticket on the committed artifact (tests/test_api.py)
assert abs(body["probability"] - 0.756159) < 1e-4, body
'

r=$(invoke "${EVENTS}/score_invalid.json")
check score_invalid "$r" '
assert resp["statusCode"] == 422, resp
assert body["detail"][0]["loc"] == ["body", "severity"], body
'

now_ms() { python3 -c "import time; print(int(time.time() * 1000))"; }  # macOS date has no %N
t0=$(now_ms)
for _ in 1 2 3 4 5; do invoke "${EVENTS}/score.json" >/dev/null; done
t1=$(now_ms)
echo "==> warm /v1/score round trip through the emulator: $(( (t1 - t0) / 5 )) ms (mean of 5)"

status=0
echo "==> smoke test passed for ${IMAGE}"
