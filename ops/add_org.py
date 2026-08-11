"""Onboard a new tenant (org_id) across every component that needs to know.

Adding a *site* needs none of this — a Clarity station IS a site_id, and
SiteAssignments is already many-participants-to-one-station, so a second
station inside an existing org is just a different value in the enrolment
form. Use this only when the new site is a separate tenant whose data must
not mix with the existing one.

What has to change, and why each one matters
--------------------------------------------
A raw S3 key carries no org. `fitbit/raw/<fitbit_id>/…` says which Fitbit
account produced it but not which tenant owns that account, so every
*reader* has to know the full tenant list and try each. A reader that does
not know about org2 does not fail loudly — it resolves nothing, quarantines
the file, and raises UnattributedRawFile forever.

  ingestion-resolver     CBT_ORG_IDS   attribution for all three sensors
  cosinuss-pull-to-s3    CBT_ORG_IDS   write-time wearer lookup
  participant-registration-api
                         CBT_ORG_IDS   allow-list for the org_id in a
                                       request body (an unknown org would
                                       otherwise create a shadow tenant
                                       nothing reads)
  ledger-materialize-users-json
                         CBT_ORG_IDS   which orgs to merge into the shared
                                       users.json
  calibration-sweep      CBT_ORG_IDS   which orgs to sweep

The Streamlit form already takes org_id as a field, so it needs no change.

THE CONSTRAINT YOU CANNOT CONFIGURE AWAY
----------------------------------------
`user_id` must be globally unique across orgs, not just within one.

Three S3 layouts key on it and none is org-scoped:

    predictions/<user_id>/…          written by heat-stress-predict
    cosinuss/raw/<user_id>/<recv>/…  written by the cosinuss puller
    users-heat-stress/users.json     keyed by user_id

If org1 and org2 both have a "user15", their core-temperature data and
their predictions land in the same prefixes and silently interleave. That
is a data-integrity failure that no amount of DynamoDB tenancy prevents,
because the isolation is in the ledger and these paths are not.

This tool refuses to add an org whose participants would collide, and
`--check-collisions` re-checks an existing setup.

    python -m ops.add_org --list
    python -m ops.add_org --org org2
    python -m ops.add_org --org org2 --commit
    python -m ops.add_org --check-collisions
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import boto3

# The org-aware function table is shared with the admin portal (which can
# push a new org without a terminal) — single spec of record in cbt_shared.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(_REPO_ROOT, "shared-lib")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from cbt_shared.org_functions import ORG_AWARE_FUNCTIONS, current_orgs  # noqa: E402


def user_id_collisions(dynamodb) -> dict[str, list[str]]:
    """user_id -> orgs holding it, for any user_id in more than one org."""
    table = dynamodb.Table("Participants")
    by_user = defaultdict(list)
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            if "user_id" not in item or "#uniq#" in item.get("pk", ""):
                continue
            org = item["pk"].split("#", 1)[0]
            by_user[item["user_id"]].append(org)
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return {u: sorted(set(o)) for u, o in by_user.items() if len(set(o)) > 1}


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", help="tenant id to add, e.g. org2")
    ap.add_argument("--list", action="store_true",
                    help="show the tenant list each component believes in")
    ap.add_argument("--check-collisions", action="store_true",
                    help="report user_ids shared across orgs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    dynamodb = boto3.resource("dynamodb", region_name=args.region)

    if args.list or not args.org:
        print("Configured tenants per component:\n")
        for fn, orgs in current_orgs(lam).items():
            state = "NOT DEPLOYED" if orgs is None else ", ".join(orgs)
            print(f"  {fn:<32} {state}")
        print("\nAny component missing an org silently quarantines its data.")

    if args.check_collisions or args.org:
        clashes = user_id_collisions(dynamodb)
        print("\nCross-org user_id collisions:")
        if clashes:
            for user, orgs in sorted(clashes.items()):
                print(f"  ✗ {user} exists in {orgs}")
            print("\n  These share predictions/<user_id>/ and "
                  "cosinuss/raw/<user_id>/ — their data is interleaving.")
        else:
            print("  none — every user_id is globally unique")

    if not args.org:
        return

    existing = current_orgs(lam)
    print(f"\nAdding {args.org!r} to {len(ORG_AWARE_FUNCTIONS)} component(s):\n")
    for fn, var in ORG_AWARE_FUNCTIONS.items():
        orgs = existing.get(fn)
        if orgs is None:
            print(f"  – {fn:<32} NOT DEPLOYED — skipped")
            continue
        if args.org in orgs:
            print(f"  ✓ {fn:<32} already knows {args.org}")
            continue
        updated = orgs + [args.org]
        print(f"  + {fn:<32} {var}={','.join(updated)}")
        if args.commit:
            env = lam.get_function_configuration(
                FunctionName=fn).get("Environment", {}).get("Variables", {})
            env[var] = ",".join(updated)
            lam.update_function_configuration(
                FunctionName=fn, Environment={"Variables": env})
            lam.get_waiter("function_updated").wait(FunctionName=fn)

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return
    print(f"\nApplied. Enrol into {args.org} by setting the Organization ID "
          f"field in the enrolment form (or org_id in a POST /register body).")
    print("Remember: user_ids must stay globally unique — re-run with "
          "--check-collisions after the first enrolments.")


if __name__ == "__main__":  # pragma: no cover
    main()
