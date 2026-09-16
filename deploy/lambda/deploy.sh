#!/usr/bin/env bash
# Build, push and deploy the slawatch scoring API as a Lambda container image with a public
# Function URL. Draft for step 5 -- NOT YET EXECUTED against an AWS account. Read docs/deploy.md
# first; the manual commands there are the reference, this script is their idempotent form.
#
# Every step is create-or-update, so re-running after a partial failure is safe. Needs the AWS
# CLI v2 with credentials for the target account, Docker with buildx, git and jq.
#
#   AWS_REGION=ca-central-1 deploy/lambda/deploy.sh
#
# Parameters (environment variables, all optional):
#   AWS_REGION            default ca-central-1
#   FUNCTION_NAME         default slawatch-api
#   ECR_REPO              default slawatch-api
#   ROLE_NAME             default slawatch-api-lambda-role
#   PLATFORM / ARCH       default linux/arm64 / arm64  (use linux/amd64 / x86_64 for Intel)
#   MEMORY_MB             default 1024
#   TIMEOUT_S             default 15
#   RESERVED_CONCURRENCY  default 2       (hard cap on parallel executions = cost guard)
#   LOG_RETENTION_DAYS    default 14
#   TAG                   default: short git SHA of HEAD
#   SKIP_BUILD=1          reuse the image already in ECR under TAG
set -euo pipefail

AWS_REGION="${AWS_REGION:-ca-central-1}"
FUNCTION_NAME="${FUNCTION_NAME:-slawatch-api}"
ECR_REPO="${ECR_REPO:-slawatch-api}"
ROLE_NAME="${ROLE_NAME:-slawatch-api-lambda-role}"
PLATFORM="${PLATFORM:-linux/arm64}"
ARCH="${ARCH:-arm64}"
MEMORY_MB="${MEMORY_MB:-1024}"
TIMEOUT_S="${TIMEOUT_S:-15}"
RESERVED_CONCURRENCY="${RESERVED_CONCURRENCY:-2}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
BASIC_EXEC_POLICY="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
TAG="${TAG:-$(git -C "${REPO_ROOT}" rev-parse --short HEAD)}"

for tool in aws docker git jq; do
  command -v "${tool}" >/dev/null || { echo "missing: ${tool}" >&2; exit 1; }
done

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${ECR_REPO}:${TAG}"
echo "==> account ${AWS_ACCOUNT_ID}, region ${AWS_REGION}, image ${IMAGE_URI}, ${ARCH}"

# 1. ECR repository + lifecycle policy (keep the last 3 images)
if ! aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "==> creating ECR repository ${ECR_REPO}"
  aws ecr create-repository --repository-name "${ECR_REPO}" \
    --image-scanning-configuration scanOnPush=true --image-tag-mutability MUTABLE \
    --region "${AWS_REGION}" >/dev/null
fi
aws ecr put-lifecycle-policy --repository-name "${ECR_REPO}" --region "${AWS_REGION}" \
  --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep last 3","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":3},"action":{"type":"expire"}}]}' \
  >/dev/null

# 2. Build and push (single-platform manifest: Lambda rejects OCI indexes with attestations)
if [ -z "${SKIP_BUILD:-}" ]; then
  echo "==> building ${PLATFORM} image and pushing ${IMAGE_URI}"
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${REGISTRY}"
  docker buildx build --platform "${PLATFORM}" --provenance=false --sbom=false \
    -f "${HERE}/Dockerfile" -t "${IMAGE_URI}" --push "${REPO_ROOT}"
fi

# 3. Execution role: CloudWatch Logs only
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "==> creating role ${ROLE_NAME}"
  aws iam create-role --role-name "${ROLE_NAME}" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  echo "    waiting 10 s for IAM propagation"
  sleep 10
fi
aws iam attach-role-policy --role-name "${ROLE_NAME}" --policy-arn "${BASIC_EXEC_POLICY}"
ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text)"

# 4. Function: create or update code, then configuration
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "==> updating ${FUNCTION_NAME} to ${IMAGE_URI}"
  aws lambda update-function-code --function-name "${FUNCTION_NAME}" --image-uri "${IMAGE_URI}" \
    --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
  aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" \
    --memory-size "${MEMORY_MB}" --timeout "${TIMEOUT_S}" --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
else
  echo "==> creating ${FUNCTION_NAME} (${ARCH}, ${MEMORY_MB} MB, ${TIMEOUT_S} s)"
  aws lambda create-function --function-name "${FUNCTION_NAME}" \
    --package-type Image --code ImageUri="${IMAGE_URI}" --role "${ROLE_ARN}" \
    --architectures "${ARCH}" --memory-size "${MEMORY_MB}" --timeout "${TIMEOUT_S}" \
    --description "slawatch SLA-breach scoring API (synthetic data)" \
    --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
fi
aws lambda put-function-concurrency --function-name "${FUNCTION_NAME}" \
  --reserved-concurrent-executions "${RESERVED_CONCURRENCY}" --region "${AWS_REGION}" >/dev/null

# 5. Log retention
aws logs create-log-group --log-group-name "/aws/lambda/${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "/aws/lambda/${FUNCTION_NAME}" \
  --retention-in-days "${LOG_RETENTION_DAYS}" --region "${AWS_REGION}"

# 6. Public Function URL
if ! aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "==> creating Function URL (auth NONE)"
  aws lambda create-function-url-config --function-name "${FUNCTION_NAME}" --auth-type NONE \
    --region "${AWS_REGION}" >/dev/null
fi
aws lambda add-permission --function-name "${FUNCTION_NAME}" --statement-id public-function-url \
  --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE \
  --region "${AWS_REGION}" >/dev/null 2>&1 || true
API_URL="$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" --query FunctionUrl --output text)"

# 7. Verify
echo "==> ${API_URL}"
curl -sS --retry 5 --retry-all-errors --retry-delay 2 "${API_URL}health" | jq .
curl -sS -X POST "${API_URL}v1/score" -H 'content-type: application/json' \
  -d "$(jq -r .body "${HERE}/events/score.json")" | jq .
echo "==> docs: ${API_URL}docs"
