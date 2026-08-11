"""The Lambdas that must know the full tenant (org_id) list.

Spec of record, shared by the admin portal (which pushes a new org to
these functions automatically) and ops/add_org.py (the CLI equivalent).
It lives in cbt_shared — not ops/ — because the Streamlit apps deploy
from the git checkout and must not import operator-only tooling.

A raw S3 key carries no org, so every *reader* has to know the full
tenant list and try each; a reader missing an org does not fail loudly —
it silently quarantines that org's data. Missing one function here is the
same silent failure, which is why this is a table rather than five places
to remember.
"""

from __future__ import annotations

# function name -> the env var holding its comma-separated tenant list
ORG_AWARE_FUNCTIONS = {
    "ingestion-resolver": "CBT_ORG_IDS",
    "cosinuss-pull-to-s3": "CBT_ORG_IDS",
    "participant-registration-api": "CBT_ORG_IDS",
    "ledger-materialize-users-json": "CBT_ORG_IDS",
    "calibration-sweep": "CBT_ORG_IDS",
}


def current_orgs(lambda_client) -> dict[str, list[str] | None]:
    """What each function currently believes the tenant list is.
    None = function not deployed / not reachable."""
    out: dict[str, list[str] | None] = {}
    for fn, var in ORG_AWARE_FUNCTIONS.items():
        try:
            env = lambda_client.get_function_configuration(
                FunctionName=fn).get("Environment", {}).get("Variables", {})
        except Exception:
            out[fn] = None
            continue
        raw = env.get(var) or env.get("CBT_ORG_ID") or ""
        out[fn] = [o.strip() for o in raw.split(",") if o.strip()]
    return out
