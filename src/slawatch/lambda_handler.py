"""AWS Lambda entry point: the FastAPI app behind an API Gateway HTTP API (v2.0 events) or a
Lambda Function URL, adapted by Mangum.

``slawatch.api`` loads the model when it is imported, i.e. during function init, so a missing
or mismatched artifact fails the cold start with a clear error in CloudWatch instead of a 500
on the first request. Lifespan events are off for the same reason: there is nothing left to
do at startup.
"""

from __future__ import annotations

from mangum import Mangum

from slawatch.api import app

handler = Mangum(app, lifespan="off")
