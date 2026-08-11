import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "services", "admin-portal"))

from admin_service import org_rollout_status, sync_org_to_lambdas  # noqa: E402
from cbt_shared.org_functions import ORG_AWARE_FUNCTIONS


class _Waiter:
    def wait(self, **_):
        pass


class FakeLambda:
    """Just enough of the Lambda control-plane API for the sync path."""

    def __init__(self, envs):
        self.envs = envs  # fn -> Variables dict, missing fn = not deployed

    def get_function_configuration(self, FunctionName):
        if FunctionName not in self.envs:
            raise RuntimeError("ResourceNotFoundException")
        return {"Environment": {"Variables": dict(self.envs[FunctionName])}}

    def update_function_configuration(self, FunctionName, Environment):
        self.envs[FunctionName] = dict(Environment["Variables"])
        return {}

    def get_waiter(self, _name):
        return _Waiter()


def test_sync_appends_org_everywhere_and_is_idempotent():
    fns = list(ORG_AWARE_FUNCTIONS)
    lam = FakeLambda({fn: {"CBT_ORG_IDS": "org1", "SECRET": "keep-me"}
                      for fn in fns})

    results = sync_org_to_lambdas(lam, "org2")
    assert {r["status"] for r in results} == {"added"}
    for fn in fns:
        assert lam.envs[fn]["CBT_ORG_IDS"] == "org1,org2"
        assert lam.envs[fn]["SECRET"] == "keep-me"  # env merged, not replaced

    # Second run is a no-op, not a duplicate append.
    again = sync_org_to_lambdas(lam, "org2")
    assert {r["status"] for r in again} == {"already"}
    assert lam.envs[fns[0]]["CBT_ORG_IDS"] == "org1,org2"


def test_sync_reads_legacy_singular_var_and_reports_missing_functions():
    fns = list(ORG_AWARE_FUNCTIONS)
    envs = {fn: {"CBT_ORG_IDS": "org1"} for fn in fns[1:]}
    envs[fns[1]] = {"CBT_ORG_ID": "org1"}  # legacy singular key
    del envs[fns[2]]  # not deployed
    lam = FakeLambda(envs)

    results = {r["function"]: r["status"]
               for r in sync_org_to_lambdas(lam, "org2")}
    assert results[fns[0]] == "not_deployed"
    assert results[fns[1]] == "added"
    assert lam.envs[fns[1]]["CBT_ORG_IDS"] == "org1,org2"
    assert results[fns[2]] == "not_deployed"

    status = {r["function"]: r["status"]
              for r in org_rollout_status(lam, "org2")}
    assert status[fns[1]] == "known"
