import boto3
import pytest
from moto import mock_aws

from assignment_ledger import create_tables
from cbt_shared.tenancy import ScopedTable

ORG = "org1"


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def dynamodb(aws):
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    create_tables(resource)
    return resource


@pytest.fixture
def dynamo_client(aws, dynamodb):
    return boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def participants(dynamodb):
    return ScopedTable(dynamodb.Table("Participants"), ORG)


@pytest.fixture
def devices(dynamodb):
    return ScopedTable(dynamodb.Table("DeviceAssignments"), ORG)


@pytest.fixture
def sites(dynamodb):
    return ScopedTable(dynamodb.Table("SiteAssignments"), ORG)


@pytest.fixture
def calibration_table(dynamodb):
    return ScopedTable(dynamodb.Table("CalibrationHistory"), ORG)


@pytest.fixture
def s3(aws):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="raw-data-all-sensors-test")
    client.create_bucket(Bucket="users-heat-stress-test")
    return client
