# Deploying the scoring API to AWS Lambda — draft runbook

> **Status: executed 2026-09-16.** The service is live behind an **API Gateway HTTP API** in
> `ca-central-1`. What was created, the live checks, and the latency figures are in
> [`evidence/aws-deploy-2026-09-16.md`](evidence/aws-deploy-2026-09-16.md). `deploy.sh`
> defaults to the HTTP API (`ENDPOINT=httpapi`); `ENDPOINT=url` gives the Function URL path.
> Reserved concurrency could not be set on a new account (quota 10); see the evidence file.

The service is [`slawatch.api`](../src/slawatch/api.py) (FastAPI) wrapped by Mangum in
[`slawatch.lambda_handler`](../src/slawatch/lambda_handler.py), packaged as a Lambda
**container image** from [`deploy/lambda/Dockerfile`](../deploy/lambda/Dockerfile). The model
it serves is the committed artifact `models/sla_breach.joblib`, verified at init against
`models/model_card.json` (version, thresholds, `trained_at`, byte size). The model was trained
on **synthetic** data; the API says so in its OpenAPI description and `/health`.

## What gets created

| Resource | Name | Notes |
|---|---|---|
| ECR repository | `slawatch-api` | private; one image per git SHA, lifecycle policy keeps the last 3 |
| IAM role | `slawatch-api-lambda-role` | trust `lambda.amazonaws.com`; only `AWSLambdaBasicExecutionRole` (CloudWatch Logs) |
| Lambda function | `slawatch-api` | image package, **arm64**, 1024 MB, 15 s timeout, reserved concurrency 2 |
| Lambda Function URL | on the function | auth `NONE` (public demo endpoint); see the choice below |
| CloudWatch log group | `/aws/lambda/slawatch-api` | retention 14 days |
| *(alternative)* API Gateway HTTP API | `slawatch-api` | `$default` stage, Lambda proxy integration (payload v2.0), route throttling |

Everything is in `ca-central-1`. Region and names are parameters of the scripts.

### Why a Function URL rather than API Gateway (recommendation)

Both hand Mangum the same API Gateway **v2.0** event, so the code does not change either
way (the local smoke test uses exactly that shape). For a public portfolio endpoint:

* **Function URL** — one extra resource on the function itself, no separate service, no
  per-request cost beyond Lambda, HTTPS with an AWS-issued hostname
  (`https://<id>.lambda-url.ca-central-1.on.aws/`). Throttling is by **reserved concurrency**
  on the function (set to 2 here), which caps spend hard. CORS can be configured on the URL
  if the dashboard ever calls it from a browser.
* **HTTP API** — needed for a custom domain, JWT authorisers, per-route throttling
  (`ThrottlingBurstLimit` / `ThrottlingRateLimit`), or usage metering. It is $1.00 per
  million requests after the 12-month free tier and adds an API, stage, integration and
  permission to manage and tear down.

Decision: the deployment uses the **HTTP API**, for its stage throttling and because it is the
more common production pattern. The original draft recommended a **Function URL first** (simplest thing that works, cheapest, least to tear
down), with reserved concurrency as the cost guard and a CloudWatch alarm on invocations.
Switch to the HTTP API only if a custom domain or real throttling becomes necessary; the
commands for that path are included below.

### Memory and timeout

The runtime imports numpy, pandas, scipy and scikit-learn at init. Lambda CPU scales with
memory, so 1024 MB is a cold-start choice, not a working-set one (a warm invocation uses well
under 200 MB). Timeout 15 s: a warm `/v1/score` is milliseconds and a 500-ticket batch is
well under a second; 15 s leaves room for a cold start behind the first request while
staying under the 30 s API Gateway limit should that path be used. Measured cold and warm
times from the local emulator are in the step-4 PR; re-measure on Lambda and revisit
(512 MB may be enough).

## Prerequisites

* AWS CLI v2 configured for the target account with rights to ECR, Lambda, IAM (role
  creation and `AWSLambdaBasicExecutionRole` attachment) and CloudWatch Logs.
* Docker with buildx (Apple Silicon builds `linux/arm64` natively; on an amd64 host the
  arm64 build runs under QEMU, slower but fine for this image size).
* `git`, `jq`, `curl`.

```bash
export AWS_REGION=ca-central-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO=slawatch-api
export FUNCTION_NAME=slawatch-api
export ROLE_NAME=slawatch-api-lambda-role
export TAG=$(git rev-parse --short HEAD)
export IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${TAG}"
```

[`deploy/lambda/deploy.sh`](../deploy/lambda/deploy.sh) runs steps 1–6 with these
parameters and is idempotent (create-or-update at each step);
[`deploy/lambda/teardown.sh`](../deploy/lambda/teardown.sh) reverses them. **Neither has been
run yet.** The manual steps follow so the scripts can be checked against them.

## 1. ECR repository

```bash
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository \
       --repository-name "$ECR_REPO" \
       --image-scanning-configuration scanOnPush=true \
       --image-tag-mutability MUTABLE \
       --region "$AWS_REGION"

# keep only the 3 most recent images (each is a few hundred MB; ECR's free tier is 500 MB/month for 12 months)
aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" --region "$AWS_REGION" --lifecycle-policy-text '{
  "rules": [{"rulePriority": 1, "description": "keep last 3",
             "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 3},
             "action": {"type": "expire"}}]}'
```

## 2. Build and push the arm64 image

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker buildx build \
  --platform linux/arm64 \
  --provenance=false --sbom=false \
  -f deploy/lambda/Dockerfile \
  -t "$IMAGE_URI" \
  --push .
```

`--provenance=false --sbom=false` matters: buildx otherwise pushes an OCI image index with
attestation manifests, which Lambda rejects ("image manifest, config or layer media type is
not supported"). Lambda wants a single-platform Docker or OCI image manifest.

For an **amd64** function instead (for instance if arm64 is unavailable in the account),
build with `--platform linux/amd64` and create the function with `--architectures x86_64`.
The CI job builds and smoke-tests the amd64 variant on every PR.

## 3. Execution role (least privilege)

The function needs nothing but the ability to write its logs.

```bash
aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 \
  || aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
       "Version": "2012-10-17",
       "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                      "Action": "sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
export ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
# IAM is eventually consistent; a freshly created role can take ~10 s to be assumable.
```

## 4. Lambda function (create or update)

```bash
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION_NAME" --image-uri "$IMAGE_URI" --region "$AWS_REGION"
  aws lambda wait function-updated-v2 --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
else
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --package-type Image \
    --code ImageUri="$IMAGE_URI" \
    --role "$ROLE_ARN" \
    --architectures arm64 \
    --memory-size 1024 \
    --timeout 15 \
    --description "slawatch SLA-breach scoring API (synthetic data)" \
    --region "$AWS_REGION"
  aws lambda wait function-active-v2 --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
fi

# cost guard: at most 2 concurrent executions
aws lambda put-function-concurrency --function-name "$FUNCTION_NAME" \
  --reserved-concurrent-executions 2 --region "$AWS_REGION"
```

The model path is baked into the image (`SLAWATCH_MODEL_PATH=/var/task/models/sla_breach.joblib`);
no function environment variables are needed.

Sanity check before exposing anything (an API Gateway v2.0 event, the same file the local
smoke test uses):

```bash
aws lambda invoke --function-name "$FUNCTION_NAME" --region "$AWS_REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload file://deploy/lambda/events/health.json /dev/stdout | jq .
```

## 5. Log retention

```bash
aws logs create-log-group --log-group-name "/aws/lambda/${FUNCTION_NAME}" --region "$AWS_REGION" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "/aws/lambda/${FUNCTION_NAME}" \
  --retention-in-days 14 --region "$AWS_REGION"
```

## 6a. Function URL (recommended)

```bash
aws lambda get-function-url-config --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws lambda create-function-url-config --function-name "$FUNCTION_NAME" \
       --auth-type NONE --region "$AWS_REGION"
aws lambda add-permission --function-name "$FUNCTION_NAME" \
  --statement-id public-function-url \
  --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE \
  --region "$AWS_REGION" 2>/dev/null || true   # already exists on re-run

export API_URL=$(aws lambda get-function-url-config --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION" --query FunctionUrl --output text)
curl -s "${API_URL}health" | jq .
curl -s -X POST "${API_URL}v1/score" -H 'content-type: application/json' \
  -d "$(jq -r .body deploy/lambda/events/score.json)" | jq .
open "${API_URL}docs"
```

## 6b. API Gateway HTTP API (alternative)

"Quick create" makes the API, a `$default` stage with auto-deploy, a `$default` route and a
Lambda proxy integration with payload format 2.0 in one call.

```bash
export FUNCTION_ARN=$(aws lambda get-function --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION" --query Configuration.FunctionArn --output text)
export API_ID=$(aws apigatewayv2 create-api --name slawatch-api --protocol-type HTTP \
  --target "$FUNCTION_ARN" --region "$AWS_REGION" --query ApiId --output text)
aws lambda add-permission --function-name "$FUNCTION_NAME" \
  --statement-id apigateway-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*" \
  --region "$AWS_REGION"
# throttle: 5 requests/s sustained, bursts of 10
aws apigatewayv2 update-stage --api-id "$API_ID" --stage-name '$default' \
  --default-route-settings '{"ThrottlingBurstLimit": 10, "ThrottlingRateLimit": 5}' \
  --region "$AWS_REGION"
export API_URL=$(aws apigatewayv2 get-api --api-id "$API_ID" --region "$AWS_REGION" \
  --query ApiEndpoint --output text)/
curl -s "${API_URL}health" | jq .
```

With the `$default` stage there is no stage prefix in the path, so FastAPI needs no
`root_path`. If a named stage is used instead, set `root_path="/<stage>"` on the app (or
`Mangum(app, api_gateway_base_path="/<stage>")`).

## Throttling and usage

* Reserved concurrency 2 on the function is the hard cap on parallel executions and thus
  on spend; requests beyond it get HTTP 429 from the Function URL. Raise it deliberately.
* HTTP API route throttling (6b) is the softer, per-second control; there are no usage
  plans or API keys on HTTP APIs (that is REST API territory).
* Add a billing alarm and an invocation-count alarm before sharing the URL widely:

```bash
aws cloudwatch put-metric-alarm --alarm-name slawatch-api-invocations \
  --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value="$FUNCTION_NAME" \
  --statistic Sum --period 3600 --evaluation-periods 1 --threshold 5000 \
  --comparison-operator GreaterThanThreshold --region "$AWS_REGION"
```

## Cost (free tier)

Assuming a portfolio-scale load (a few thousand requests a month):

| Item | Free tier | Expected |
|---|---|---|
| Lambda requests | 1M / month, always free | $0 |
| Lambda compute | 400,000 GB-s / month, always free (arm64 is ~20 % cheaper than x86 beyond it) | $0 — at 1024 MB that is ~400,000 s of execution |
| ECR storage | 500 MB / month for 12 months, then $0.10 / GB-month | $0 in year one if the lifecycle policy keeps the repo under ~500 MB (one image ≈ the size reported by CI); a few cents / month after |
| CloudWatch Logs | 5 GB ingestion, 5 GB storage / month | $0 with 14-day retention |
| Data transfer out | 100 GB / month | $0 |
| Function URL | no charge | $0 |
| HTTP API (6b only) | 1M requests / month for 12 months, then $1.00 / M | $0 |

Cold starts are the only user-visible cost: expect a few seconds on the first request after
idle (numpy/pandas/scikit-learn import plus model load). Provisioned concurrency would
remove that but is not free; a scheduled warm-up ping every 5 minutes is the usual cheap
alternative and stays inside the free tier (about 8,600 invocations a month).

## Teardown

Removes everything created above, in dependency order. Idempotent: each step tolerates the
resource already being gone. [`deploy/lambda/teardown.sh`](../deploy/lambda/teardown.sh) does
the same.

```bash
# Function URL and permissions
aws lambda delete-function-url-config --function-name "$FUNCTION_NAME" --region "$AWS_REGION" || true

# HTTP API (only if 6b was used)
for id in $(aws apigatewayv2 get-apis --region "$AWS_REGION" --query "Items[?Name=='slawatch-api'].ApiId" --output text); do
  aws apigatewayv2 delete-api --api-id "$id" --region "$AWS_REGION"
done

# Alarm, function, logs
aws cloudwatch delete-alarms --alarm-names slawatch-api-invocations --region "$AWS_REGION" || true
aws lambda delete-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" || true
aws logs delete-log-group --log-group-name "/aws/lambda/${FUNCTION_NAME}" --region "$AWS_REGION" || true

# Role (detach before delete)
aws iam detach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole || true
aws iam delete-role --role-name "$ROLE_NAME" || true

# ECR repository and every image in it
aws ecr delete-repository --repository-name "$ECR_REPO" --force --region "$AWS_REGION" || true

# Verify nothing is left
aws lambda list-functions --region "$AWS_REGION" --query "Functions[?FunctionName=='${FUNCTION_NAME}']"
aws ecr describe-repositories --region "$AWS_REGION" --query "repositories[?repositoryName=='${ECR_REPO}']"
aws iam get-role --role-name "$ROLE_NAME" 2>&1 | head -1
```

## Open questions for the deploy step

* Confirm arm64 Lambda is available in the account/region (it is offered in `ca-central-1`)
  and that the arm64 image cold start is acceptable; otherwise use the amd64 path.
* 1024 MB vs 512 MB: measure init duration in CloudWatch (`REPORT ... Init Duration`) and
  pick the cheaper setting that keeps cold starts under ~3 s.
* Whether the dashboard step needs CORS on the Function URL (`--cors` on
  `create-function-url-config`).
