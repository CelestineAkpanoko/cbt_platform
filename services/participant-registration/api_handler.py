"""Lambda wrapper exposing register() as POST /register (scripted/bulk use).

The Streamlit form (app.py) is the primary field UI; both call the same
registration_service.register()."""

import json
import os

import boto3

from cbt_shared.tenancy import ScopedTable
from registration_service import (
    DuplicateSubmissionError,
    RegistrationRequest,
    ValidationError,
    register,
)

ORG_ID = os.environ.get("CBT_ORG_ID", "org1")

_dynamodb = boto3.resource("dynamodb")
_client = boto3.client("dynamodb")


def lambda_handler(event, _context):
    body = json.loads(event.get("body") or "{}")
    try:
        req = RegistrationRequest(**body)
        result = register(
            _client,
            ScopedTable(_dynamodb.Table("Participants"), ORG_ID),
            ScopedTable(_dynamodb.Table("DeviceAssignments"), ORG_ID),
            ScopedTable(_dynamodb.Table("SiteAssignments"), ORG_ID),
            req,
        )
    except (TypeError, ValidationError) as e:
        return {"statusCode": 400, "body": json.dumps({"error": str(e)})}
    except DuplicateSubmissionError as e:
        return {"statusCode": 409, "body": json.dumps({"error": str(e)})}
    return {
        "statusCode": 200,
        "body": json.dumps({
            "participant_id": result.participant_id,
            "created_new_participant": result.created_new_participant,
            "matched_on": result.matched_on,
        }),
    }
