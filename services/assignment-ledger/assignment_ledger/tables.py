"""DynamoDB table definitions for the identity/assignment ledger.

Used two ways: tests create these against moto; infra/ledger-tables holds
the CloudFormation equivalents (keep in sync — the CFN templates are
generated from these specs, see infra/ledger-tables/README.md).
"""

from __future__ import annotations

TABLE_NAMES = {
    "participants": "Participants",
    "device_assignments": "DeviceAssignments",
    "site_assignments": "SiteAssignments",
    "calibration_history": "CalibrationHistory",
}

TABLE_SPECS = [
    {
        "TableName": TABLE_NAMES["participants"],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "user_id_pk", "AttributeType": "S"},
            {"AttributeName": "email_pk", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                # unique lookup: org_id#user_id -> participant
                "IndexName": "ByUserId",
                "KeySchema": [{"AttributeName": "user_id_pk", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                # unique lookup: org_id#email -> participant (sparse: legacy
                # records with no email have no email_pk and are not indexed)
                "IndexName": "ByEmail",
                "KeySchema": [{"AttributeName": "email_pk", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": TABLE_NAMES["device_assignments"],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "participant_pk", "AttributeType": "S"},
            {"AttributeName": "is_current", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "ByParticipant",
                "KeySchema": [
                    {"AttributeName": "participant_pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                # Sparse: only current rows carry is_current
                "IndexName": "Current",
                "KeySchema": [
                    {"AttributeName": "is_current", "KeyType": "HASH"},
                    {"AttributeName": "pk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "StreamSpecification": {
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
    },
    {
        "TableName": TABLE_NAMES["site_assignments"],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "site_pk", "AttributeType": "S"},
            {"AttributeName": "is_current", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "BySite",
                "KeySchema": [
                    {"AttributeName": "site_pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "Current",
                "KeySchema": [
                    {"AttributeName": "is_current", "KeyType": "HASH"},
                    {"AttributeName": "pk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
        "StreamSpecification": {
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
    },
    {
        "TableName": TABLE_NAMES["calibration_history"],
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "computed_at", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "computed_at", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
]

# NB: there is deliberately no DeviceInventory table. The raw sensor bucket
# IS the device inventory — the pull Lambdas land data under
# fitbit/raw/<id>/ and cosinuss/raw/<id>/, so every device the platform has
# ever seen appears as an S3 prefix. See
# registration_service/s3_inventory.py.


def create_tables(dynamodb_resource):
    """Create all ledger tables (test/bootstrap use)."""
    tables = {}
    for spec in TABLE_SPECS:
        tables[spec["TableName"]] = dynamodb_resource.create_table(**spec)
    for t in tables.values():
        t.wait_until_exists()
    return tables
