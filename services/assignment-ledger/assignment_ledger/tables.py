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
            {"AttributeName": "fitbit_id_pk", "AttributeType": "S"},
            {"AttributeName": "org_pk", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                # "every participant in this org" without a Scan. Sparse:
                # uniqueness markers carry no org_pk, so the index holds
                # exactly the person records.
                "IndexName": "ByOrg",
                "KeySchema": [
                    {"AttributeName": "org_pk", "KeyType": "HASH"},
                    {"AttributeName": "pk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
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
            {
                # unique lookup: org_id#fitbit_id -> participant. Replaces
                # the old "current wearer of fitbit X" DeviceAssignments
                # query — a Fitbit account belongs to exactly one person,
                # so this is a direct binding, not a time window. Sparse:
                # a participant who hasn't connected a Fitbit is unindexed.
                "IndexName": "ByFitbitId",
                "KeySchema": [{"AttributeName": "fitbit_id_pk", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "BillingMode": "PAY_PER_REQUEST",
        # fitbit_id lives on this table now, and users.json /
        # config/user_mapping.json both key on it — so the materializer has
        # to react to Participants writes, not just assignment writes.
        "StreamSpecification": {
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
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
# and the pullers' own state files ARE the device inventory — see
# registration_service/s3_inventory.py, which derives the Cosinuss receiver
# list and the Clarity station list from them. DeviceAssignments holds only
# cosinuss rows; clarity lives in SiteAssignments and fitbit on
# Participants (see cbt_shared.models.DEVICE_TYPES).


def create_tables(dynamodb_resource):
    """Create all ledger tables (test/bootstrap use)."""
    tables = {}
    for spec in TABLE_SPECS:
        tables[spec["TableName"]] = dynamodb_resource.create_table(**spec)
    for t in tables.values():
        t.wait_until_exists()
    return tables
