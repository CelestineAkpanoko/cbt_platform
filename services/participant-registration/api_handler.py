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

# Default tenant. A request may name a different one via "org_id" in the
# body, which is what makes a second site with its own tenant workable
# without a second deployment of this Lambda — the Streamlit form already
# carries org_id as a field for exactly this reason.
DEFAULT_ORG_ID = os.environ.get("CBT_ORG_ID", "org1")
# Allow-list. Without it, a typo'd or hostile org_id would silently create
# a whole shadow tenant that no reader is configured to look at: the data
# would land, resolve to nothing, and quarantine forever.
ALLOWED_ORG_IDS = {o.strip() for o in os.environ.get(
    "CBT_ORG_IDS", DEFAULT_ORG_ID).split(",") if o.strip()}

_dynamodb = boto3.resource("dynamodb")
_client = boto3.client("dynamodb")


def lambda_handler(event, _context):
    body = json.loads(event.get("body") or "{}")
    org_id = (body.pop("org_id", None) or DEFAULT_ORG_ID).strip()
    if org_id not in ALLOWED_ORG_IDS:
        return {"statusCode": 400, "body": json.dumps({
            "error": f"unknown org_id {org_id!r}. Known: "
                     f"{sorted(ALLOWED_ORG_IDS)}. Add it to CBT_ORG_IDS on "
                     f"this function and on the readers (ingestion-resolver, "
                     f"cosinuss-pull-to-s3) before enrolling into it."})}
    try:
        req = RegistrationRequest(**body)
        result = register(
            _client,
            ScopedTable(_dynamodb.Table("Participants"), org_id),
            ScopedTable(_dynamodb.Table("DeviceAssignments"), org_id),
            ScopedTable(_dynamodb.Table("SiteAssignments"), org_id),
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
