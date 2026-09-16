# syntax=docker/dockerfile:1
# slawatch scoring API as an AWS Lambda container image.
#
#   make lambda-build                      # linux/arm64 (Graviton) by default
#   make lambda-build LAMBDA_PLATFORM=linux/amd64
#   make lambda-smoke                      # runs it under the Lambda Runtime Interface Emulator
#
# Runtime only: numpy, pandas, scipy, scikit-learn, joblib, pydantic, FastAPI, Mangum
# (`uv export --extra lambda`, no dev group, no pipeline extra, so no SQLAlchemy/psycopg,
# no matplotlib). The committed artifact and its model card ride along and the service
# verifies at init that they match.
FROM public.ecr.aws/lambda/python:3.12

# Dependencies first, from the lock file, so this layer is cached across source edits. uv is
# bind-mounted from its official image for this step only, so it is not in the final image.
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.10,source=/uv,target=/usr/local/bin/uv \
    uv export --frozen --no-dev --extra lambda --no-emit-project --no-hashes \
        -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3.12 --target "${LAMBDA_TASK_ROOT}" \
        --no-cache --compile-bytecode -r /tmp/requirements.txt \
    && rm -f pyproject.toml uv.lock /tmp/requirements.txt \
    && rm -rf "${LAMBDA_TASK_ROOT}"/bin \
    # the base image has no `find`; drop the libraries' own test suites (dead weight at runtime)
    && /var/lang/bin/python3.12 -c "import pathlib, shutil, sys; \
        root = pathlib.Path(sys.argv[1]); \
        [shutil.rmtree(p) for p in root.rglob('tests') if p.is_dir()]" "${LAMBDA_TASK_ROOT}"

# The package itself (pure Python, no build step needed) and the model.
COPY src/slawatch "${LAMBDA_TASK_ROOT}/slawatch"
COPY models/sla_breach.joblib models/model_card.json "${LAMBDA_TASK_ROOT}/models/"
RUN /var/lang/bin/python3.12 -m compileall -q "${LAMBDA_TASK_ROOT}/slawatch"

ENV SLAWATCH_MODEL_PATH="${LAMBDA_TASK_ROOT}/models/sla_breach.joblib" \
    SLAWATCH_MODEL_CARD_PATH="${LAMBDA_TASK_ROOT}/models/model_card.json"

CMD ["slawatch.lambda_handler.handler"]
