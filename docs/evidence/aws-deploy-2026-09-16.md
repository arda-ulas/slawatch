# AWS deployment record — 2026-09-16

The scoring API was deployed to AWS on 2026-09-16 with `deploy/lambda/deploy.sh` (default
`ENDPOINT=httpapi`), from `main` at `42d66d9`. The image tag is that SHA. Everything below was
captured from the AWS CLI and `curl` on the same day. The model was trained on synthetic data.

## What is deployed (`ca-central-1`)

| Resource | Identifier | Configuration |
|---|---|---|
| ECR repository | `slawatch-api` | scan on push; lifecycle keeps the last 3 images |
| Image | `slawatch-api:42d66d9` | linux/arm64, 266,294,659 bytes compressed in ECR |
| IAM role | `slawatch-api-lambda-role` | only `AWSLambdaBasicExecutionRole` attached |
| Lambda function | `slawatch-api` | image package, arm64, 1024 MB, 15 s timeout, state `Active` |
| API Gateway HTTP API | `slawatch-api` (`ud9fgjqcgl`) | quick create: `$default` route and stage, auto-deploy, Lambda proxy (payload v2.0) |
| Stage throttling | `$default` | rate 5 req/s, burst 10 |
| CloudWatch log group | `/aws/lambda/slawatch-api` | 14-day retention |
| CloudWatch alarm | `slawatch-api-invocations` | Invocations > 5,000 in an hour |
| AWS Budget | `slawatch-guard-1usd` | $1/month; email on actual spend > 1 % and forecast > 100 % |

Endpoint: `https://ud9fgjqcgl.execute-api.ca-central-1.amazonaws.com/` (OpenAPI UI at `/docs`).

**Reserved concurrency is not set.** A new account has an account concurrency quota of 10
and must keep 10 unreserved, so `PutFunctionConcurrency` was rejected
(`InvalidParameterValueException`). Spend is capped by the HTTP API throttle, the account
quota of 10 concurrent executions, the invocation alarm, and the budget. `deploy.sh` now
warns and continues in this case.

## Live checks

| Request | Result |
|---|---|
| `GET /health` | 200: `status ok`, `model_version 0.1.0`, `trained_at 2026-09-16T19:49:21+00:00`, training window 2024-07 to 2025-12, `synthetic_data true` |
| `POST /v1/score` (`deploy/lambda/events/score.json` body) | 200: `probability 0.756159`, `risk_band high`, `flag_for_review true`, threshold 0.26343…, same as the local emulator |
| `POST /v1/score` with `severity=urgent` | 422, `loc ["body","severity"]`, lists the allowed values |
| `POST /v1/score/batch` (2 tickets) | 200, `count 2` |
| `GET /v1/model` | 200, model version 0.1.0 |
| `GET /docs`, `GET /openapi.json` | 200 |

## Latency (client side, from a laptop in Ontario)

- Warm `POST /v1/score`, 5 sequential calls: 0.147–0.171 s total round trip. Lambda `REPORT`
  durations for warm requests: 2–44 ms. Max memory used: 284 MB of 1024 MB.
- Cold start, forced by a configuration change: `GET /health` took 2.33 s round trip; the
  next call took 0.13 s.
- **First-ever invocation** after creation: the init phase hit Lambda's 10 s init limit
  (`INIT_REPORT Init Duration: 10000.07 ms Phase: init Status: timeout`). The first uncached
  container-image pull is slow. Lambda re-ran init inside the request, which completed in
  4,371 ms, and the request succeeded. Later cold starts did not repeat this.

## Teardown

`CONFIRM=yes deploy/lambda/teardown.sh` removes the HTTP API, alarm, function, log group,
role and ECR repository, then checks that nothing is left. The budget is account-level and
is kept deliberately. Run the teardown and append its output here when the endpoint is
retired.
