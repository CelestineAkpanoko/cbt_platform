"""Admin portal — manage the Registry dropdowns and participants.

What lives here (password-gated, research admins only):

  Organizations     add/deactivate org entries (id + name + location).
                    These feed the org dropdown on the enrollment form.
                    NB: Lambda readers learn about a new org via
                    `python -m ops.add_org --org <id> --commit`, not here —
                    this portal deliberately has no Lambda permissions.
  Cosinuss devices  add/deactivate receiver ids for the enrollment
                    dropdown (replaces guessing from S3 prefixes).
  Clarity stations  same, for site_id.
  Participants      list an org's participants; delete one profile and all
                    of its DynamoDB rows (assignments, calibration,
                    uniqueness markers) with typed confirmation. S3 raw
                    data and predictions are NEVER deleted from here.

Config resolves from st.secrets first (Streamlit Cloud), then env vars.
Required: ADMIN_PASSWORD. Optional: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
(else ambient creds), AWS_REGION.
"""

import hmac
import os
import sys

# Monorepo imports, same pattern as the registration app.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in ("shared-lib", "services/assignment-ledger",
           "services/participant-registration", "services/admin-portal"):
    _full = os.path.join(_REPO_ROOT, _p)
    if _full not in sys.path:
        sys.path.insert(0, _full)

import boto3
import streamlit as st

try:
    from cbt_shared.tenancy import ScopedTable
    from registration_service.registry import (
        REGISTRY_TABLE_NAME,
        RegistryError,
        add_entry,
        deactivate_entry,
        list_entries,
    )
    from admin_service import (
        ParticipantNotFoundError,
        delete_participant,
        list_participants,
    )
except ImportError as _exc:
    st.error(
        "**This app is running a stale copy of the code.**\n\n"
        f"`{_exc}`\n\n"
        "Reboot the app from the Streamlit Cloud dashboard (Manage app → "
        "⋮ → Reboot) so it re-clones the repository."
    )
    st.stop()

st.set_page_config(page_title="CBT Admin Portal", page_icon="🛠️")


def conf(key, default=None):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
_ADMIN_PASSWORD = conf("ADMIN_PASSWORD")
if not _ADMIN_PASSWORD:
    st.error("ADMIN_PASSWORD is not configured. This portal refuses to run "
             "without it — set it in Streamlit secrets and reboot.")
    st.stop()

if not st.session_state.get("authed"):
    st.title("🛠️ CBT Admin Portal")
    pw = st.text_input("Admin password", type="password")
    if st.button("Sign in"):
        if hmac.compare_digest(pw, str(_ADMIN_PASSWORD)):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

# ---------------------------------------------------------------------------
# AWS wiring
# ---------------------------------------------------------------------------
AWS_REGION = conf("AWS_REGION", "us-east-1")
_ak, _sk = conf("AWS_ACCESS_KEY_ID"), conf("AWS_SECRET_ACCESS_KEY")
if _ak and _sk:
    _session = boto3.Session(aws_access_key_id=_ak,
                             aws_secret_access_key=_sk,
                             region_name=AWS_REGION)
else:
    _session = boto3.Session(region_name=AWS_REGION)
_dynamodb = _session.resource("dynamodb")
_registry = _dynamodb.Table(REGISTRY_TABLE_NAME)


def _scoped_tables(org_id: str):
    return (
        ScopedTable(_dynamodb.Table("Participants"), org_id),
        ScopedTable(_dynamodb.Table("DeviceAssignments"), org_id),
        ScopedTable(_dynamodb.Table("SiteAssignments"), org_id),
        ScopedTable(_dynamodb.Table("CalibrationHistory"), org_id),
    )


def _registry_section(kind: str, label: str, *, with_location: bool):
    """Shared list/add/deactivate UI for one registry kind."""
    entries = list_entries(_registry, kind, include_inactive=True)
    active = [e for e in entries if e.get("active", True)]
    inactive = [e for e in entries if not e.get("active", True)]

    if active:
        st.dataframe(
            [{"id": e["sk"], "name": e.get("name", ""),
              "location": e.get("location", ""),
              "added": (e.get("created_at") or "")[:10]} for e in active],
            hide_index=True, use_container_width=True)
    else:
        st.info(f"No active {label} yet — add the first one below.")

    with st.form(f"add_{kind}"):
        st.markdown(f"**Add a {label}**")
        new_id = st.text_input("ID", key=f"{kind}_id")
        new_name = st.text_input("Name", key=f"{kind}_name")
        new_loc = (st.text_input("Location", key=f"{kind}_loc")
                   if with_location else "")
        if st.form_submit_button("Add"):
            try:
                add_entry(_registry, kind, new_id, name=new_name,
                          location=new_loc, created_by="admin-portal")
                st.success(f"Added {new_id}. It appears on the enrollment "
                           "form within ~5 minutes (dropdown cache).")
                st.rerun()
            except RegistryError as exc:
                st.error(str(exc))

    if active:
        col1, col2 = st.columns([3, 1])
        with col1:
            to_remove = st.selectbox(
                f"Deactivate a {label}", [e["sk"] for e in active],
                index=None, key=f"{kind}_deact",
                help="Soft delete: hides it from the enrollment dropdown; "
                     "history is kept and it can be re-added later.")
        with col2:
            st.write("")
            if st.button("Deactivate", key=f"{kind}_deact_btn",
                         disabled=to_remove is None):
                deactivate_entry(_registry, kind, to_remove)
                st.rerun()
    if inactive:
        with st.expander(f"{len(inactive)} deactivated"):
            st.write(", ".join(e["sk"] for e in inactive))


st.title("🛠️ CBT Admin Portal")
tab_orgs, tab_cos, tab_clarity, tab_people = st.tabs(
    ["Organizations", "Cosinuss devices", "Clarity stations", "Participants"])

with tab_orgs:
    _registry_section("org", "organization", with_location=True)
    st.warning(
        "Adding an org here only feeds the enrollment dropdown. The Lambda "
        "readers (ingestion, pullers, calibration) must also learn the new "
        "tenant: run `python -m ops.add_org --org <id> --commit` — until "
        "then, that org's data is silently quarantined.", icon="⚠️")

with tab_cos:
    _registry_section("cosinuss", "Cosinuss receiver", with_location=False)

with tab_clarity:
    _registry_section("clarity", "Clarity station", with_location=True)

with tab_people:
    orgs = [e["sk"] for e in list_entries(_registry, "org")]
    if not orgs:
        st.info("Add an organization first — participants are listed per org.")
        st.stop()
    org_id = st.selectbox("Organization", orgs)
    participants, devices, sites, calibration = _scoped_tables(org_id)

    people = list_participants(participants)
    if not people:
        st.info(f"No participants in {org_id}.")
        st.stop()

    st.dataframe(
        [{"user_id": p.get("user_id"), "participant_id": p["participant_id"],
          "email": p.get("email", ""), "fitbit_id": p.get("fitbit_id", ""),
          "mode": p.get("enrollment_mode", ""),
          "enrolled": (p.get("enrolled_at") or p.get("created_at") or "")[:10]}
         for p in people],
        hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Delete a participant")
    by_pid = {f"{p.get('user_id') or '?'} ({p['participant_id']})": p
              for p in people}
    choice = st.selectbox("Participant", list(by_pid), index=None)
    if choice:
        person = by_pid[choice]
        pid = person["participant_id"]
        st.error(
            "This permanently removes the profile, device/site assignment "
            "history, calibration records, and identity markers — the "
            "user_id, email, and Fitbit account become claimable by a new "
            "registration. **S3 raw data and predictions are not deleted.**",
            icon="🗑️")
        confirm = st.text_input(
            f"Type the participant_id (`{pid}`) to confirm")
        if st.button("Delete participant and all associated data",
                     type="primary", disabled=confirm != pid):
            try:
                counts = delete_participant(
                    participants, devices, sites, calibration, pid)
            except ParticipantNotFoundError:
                st.warning("Already deleted.")
            else:
                st.success("Deleted. Rows removed: " + ", ".join(
                    f"{k}={v}" for k, v in counts.items() if v))
                st.rerun()
