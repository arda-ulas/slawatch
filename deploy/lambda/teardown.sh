#!/usr/bin/env bash
# Remove everything deploy/lambda/deploy.sh (or docs/deploy.md) creates, in dependency order.
# Draft for step 5 -- NOT YET EXECUTED. Each step tolerates the resource already being gone,
# so it can be re-run. Deleting the ECR repository removes every image in it.
#
#   AWS_REGION=ca-central-1 deploy/lambda/teardown.sh          # asks for confirmation
#   CONFIRM=yes deploy/lambda/teardown.sh                       # non-interactive
#
# Same parameters as deploy.sh: AWS_REGION, FUNCTION_NAME, ECR_REPO, ROLE_NAME.
set -euo pipefail

AWS_REGION="${AWS_REGION:-ca-central-1}"
FUNCTION_NAME="${FUNCTION_NAME:-slawatch-api}"
ECR_REPO="${ECR_REPO:-slawatch-api}"
ROLE_NAME="${ROLE_NAME:-slawatch-api-lambda-role}"
API_NAME="${API_NAME:-slawatch-api}"   # only exists if the HTTP API alternative was used
ALARM_NAME="${ALARM_NAME:-slawatch-api-invocations}"
BASIC_EXEC_POLICY="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

command -v aws >/dev/null || { echo "missing: aws" >&2; exit 1; }
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "==> tearing down ${FUNCTION_NAME} / ${ECR_REPO} / ${ROLE_NAME} in ${AWS_ACCOUNT_ID} ${AWS_REGION}"
if [ "${CONFIRM:-}" != "yes" ]; then
  read -r -p "Type 'yes' to continue: " answer
  [ "${answer}" = "yes" ] || { echo "aborted"; exit 1; }
fi

echo "==> Function URL"
aws lambda delete-function-url-config --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo "==> HTTP API (if any)"
for id in $(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
              --query "Items[?Name=='${API_NAME}'].ApiId" --output text 2>/dev/null); do
  aws apigatewayv2 delete-api --api-id "${id}" --region "${AWS_REGION}" && echo "    deleted API ${id}"
done

echo "==> CloudWatch alarm"
aws cloudwatch delete-alarms --alarm-names "${ALARM_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo "==> Lambda function"
aws lambda delete-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo "==> log group"
aws logs delete-log-group --log-group-name "/aws/lambda/${FUNCTION_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo "==> IAM role"
aws iam detach-role-policy --role-name "${ROLE_NAME}" --policy-arn "${BASIC_EXEC_POLICY}" 2>/dev/null || true
aws iam delete-role --role-name "${ROLE_NAME}" 2>/dev/null || true

echo "==> ECR repository (with all images)"
aws ecr delete-repository --repository-name "${ECR_REPO}" --force --region "${AWS_REGION}" >/dev/null 2>&1 || true

echo "==> verifying"
left=0
aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1 && { echo "    function still exists"; left=1; }
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" >/dev/null 2>&1 && { echo "    ECR repo still exists"; left=1; }
aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1 && { echo "    role still exists"; left=1; }
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${FUNCTION_NAME}" --region "${AWS_REGION}" \
  --query 'logGroups[].logGroupName' --output text 2>/dev/null | grep -q . && { echo "    log group still exists"; left=1; }
[ "${left}" -eq 0 ] && echo "==> nothing left" || { echo "==> some resources remain; re-run or remove by hand" >&2; exit 1; }
