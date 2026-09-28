"""Admin portal — manage the Registry dropdowns and participants.

What lives here (password-gated, research admins only):

  Organizations     add/deactivate org entries (id + name + location).
                    These feed the org dropdown on the enrollment form.
                    Adding an org ALSO pushes it to every org-aware Lambda
                    automatically (same merge as `ops/add_org.py --commit`),
                    with a status board + retry button — admins never need
                    a terminal.
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

import datetime
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
    from assignment_ledger.writes import DeviceJustReassignedError
    from registration_service.registry import (
        REGISTRY_TABLE_NAME,
        RegistryError,
        add_entry,
        deactivate_entry,
        list_entries,
    )
    from admin_service import (
        ParticipantNotFoundError,
        calibration_overview,
        current_assignments,
        delete_participant,
        list_participants,
        org_rollout_status,
        sync_org_to_lambdas,
        trigger_calibration_sweep,
        unassign_participant,
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
_dynamo_client = _session.client("dynamodb")
_lambda = _session.client("lambda")
_registry = _dynamodb.Table(REGISTRY_TABLE_NAME)


_SYNC_LABELS = {"added": "✅ updated", "already": "✅ already knew it",
                "known": "✅ knows it", "missing": "❌ MISSING",
                "not_deployed": "⚪ not deployed", "error": "⚠️ error"}


def _show_sync_results(rows, org_id):
    ok = all(r["status"] in ("added", "already", "known", "not_deployed")
             for r in rows)
    for r in rows:
        line = f"{_SYNC_LABELS.get(r['status'], r['status'])} — `{r['function']}`"
        if r.get("detail") and r["status"] == "error":
            line += f": {r['detail']}"
        st.write(line)
    if ok:
        st.success(f"All services now accept data for **{org_id}**. "
                   "New registrations and sensor files for this org will "
                   "attribute correctly.")
    else:
        st.error("Some services could NOT be updated (see above). Data for "
                 f"{org_id} will be quarantined by those services until "
                 "this is fixed — press “Sync services” below to retry, or "
                 "contact the engineering team if the error persists.")


def _scoped_tables(org_id: str):
    return (
        ScopedTable(_dynamodb.Table("Participants"), org_id),
        ScopedTable(_dynamodb.Table("DeviceAssignments"), org_id),
        ScopedTable(_dynamodb.Table("SiteAssignments"), org_id),
        ScopedTable(_dynamodb.Table("CalibrationHistory"), org_id),
    )


def _registry_section(kind: str, label: str, *, with_location: bool,
                      with_org: bool = False):
    """Shared list/add/deactivate UI for one registry kind."""
    entries = list_entries(_registry, kind, include_inactive=True)
    active = [e for e in entries if e.get("active", True)]
    inactive = [e for e in entries if not e.get("active", True)]

    if active:
        rows = [{"id": e["sk"], "name": e.get("name", ""),
                 "location": e.get("location", ""),
                 "added": (e.get("created_at") or "")[:10]} for e in active]
        if with_org:
            for row, e in zip(rows, active):
                row["organization"] = e.get("org_id", "⚠️ none")
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.info(f"No active {label} yet — add the first one below.")

    with st.form(f"add_{kind}"):
        st.markdown(f"**Add a {label}**")
        new_id = st.text_input("ID", key=f"{kind}_id")
        new_name = st.text_input("Name", key=f"{kind}_name")
        new_loc = (st.text_input("Location", key=f"{kind}_loc")
                   if with_location else "")
        new_org = ""
        if with_org:
            org_choices = [e["sk"] for e in list_entries(_registry, "org")]
            new_org = st.selectbox(
                "Organization this station belongs to", org_choices,
                index=None, key=f"{kind}_org",
                help="Routes this station's environmental data to the "
                     "right organization — required so data and "
                     "predictions never mix across organizations.")
        if st.form_submit_button("Add"):
            if with_org and not new_org:
                st.error("Pick the organization this station belongs to.")
                st.stop()
            try:
                add_entry(_registry, kind, new_id, name=new_name,
                          location=new_loc, org_id=new_org or "",
                          created_by="admin-portal")
            except RegistryError as exc:
                st.error(str(exc))
            else:
                if kind == "org":
                    # A new org must also be pushed to every org-aware
                    # Lambda, or its data is silently quarantined. Done
                    # here automatically so admins never need a terminal;
                    # results shown after the rerun.
                    with st.spinner("Updating platform services…"):
                        st.session_state["org_sync_results"] = (
                            new_id.strip(),
                            sync_org_to_lambdas(_lambda, new_id.strip()))
                st.success(f"Added {new_id}. It appears on the enrollment "
                           "form within ~5 minutes (dropdown cache).")
                st.rerun()

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

    # Results of the automatic service sync from the most recent add.
    if "org_sync_results" in st.session_state:
        synced_org, rows = st.session_state.pop("org_sync_results")
        st.markdown(f"**Platform services updated for `{synced_org}`:**")
        _show_sync_results(rows, synced_org)

    st.divider()
    st.subheader("Service status")
    st.caption(
        "Every backend service must know an organization before it will "
        "accept its data. Adding an org above updates them automatically — "
        "use this to double-check, or to retry if an update failed.")
    _active_orgs = [e["sk"] for e in list_entries(_registry, "org")]
    if _active_orgs:
        check_org = st.selectbox("Organization to check", _active_orgs,
                                 key="org_status_pick")
        col_a, col_b = st.columns(2)
        if col_a.button("Check status"):
            _show_sync_results(org_rollout_status(_lambda, check_org),
                               check_org)
        if col_b.button("Sync services"):
            with st.spinner("Updating platform services…"):
                _show_sync_results(sync_org_to_lambdas(_lambda, check_org),
                                   check_org)

with tab_cos:
    _registry_section("cosinuss", "Cosinuss receiver", with_location=False)

with tab_clarity:
    _registry_section("clarity", "Clarity station", with_location=True,
                      with_org=True)
    st.caption(
        "A station's organization decides where its data files go "
        "(clarity/raw/<org>/<station>/…) and which participants' "
        "predictions may use it. A station with no organization is "
        "collected under _unassigned and used by nobody — fix it by "
        "deactivating and re-adding the station with the right "
        "organization.")

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
    st.subheader("Calibration")
    st.caption(
        "Every participant with a Fitbit gets a temperature baseline from "
        "their first three nights of data after their enrollment date. The "
        "platform re-checks automatically every couple of hours; use the "
        "button to re-check right now (results appear within a few "
        "minutes — refresh the table).")
    if st.button("Show calibration status"):
        st.dataframe(calibration_overview(participants, calibration),
                     hide_index=True, use_container_width=True)
        st.caption(
            "**complete** = baselined · **extended/pending** = still "
            "accumulating nights · **waiting for first sweep** = newly "
            "(re-)registered · **no fitbit** = cannot calibrate")
    if st.button("Run calibration check now"):
        try:
            trigger_calibration_sweep(_lambda)
            st.success("Calibration check started — refresh the status "
                       "table in a few minutes.")
        except Exception as exc:
            st.error(f"Could not start the check: {exc}")

    by_pid = {f"{p.get('user_id') or '?'} ({p['participant_id']})": p
              for p in people}

    st.divider()
    st.subheader("Unassign a participant (leaving the study)")
    st.caption(
        "Ends this participant's current Cosinuss receiver and Clarity "
        "station coverage as of the date below. Their profile, calibration "
        "history, and identity markers are left untouched, so they can be "
        "re-linked later if they return. The receiver becomes selectable "
        "again on the enrollment form immediately. Use this — not delete — "
        "for someone who finished data collection but should stay on file.")
    unassign_choice = st.selectbox("Participant", list(by_pid), index=None,
                                   key="unassign_pick")
    if unassign_choice:
        u_person = by_pid[unassign_choice]
        u_pid = u_person["participant_id"]
        held = current_assignments(devices, sites, u_pid)
        if not held["devices"] and not held["site"]:
            st.info("Nothing currently assigned to this participant.")
        else:
            held_desc = [f"{d['device_type']} `{d['device_id']}`"
                        for d in held["devices"]]
            if held["site"]:
                held_desc.append(f"clarity site `{held['site']}`")
            st.write("Currently holds: " + ", ".join(held_desc))
            end_date = st.date_input("Coverage ends", value=datetime.date.today(),
                                     key="unassign_date")
            if st.button("Unassign", key="unassign_btn"):
                effective_to = datetime.datetime.combine(
                    end_date, datetime.time.min, tzinfo=datetime.timezone.utc
                ).isoformat().replace("+00:00", "Z")
                try:
                    result = unassign_participant(
                        _dynamo_client, participants, devices, sites,
                        u_pid, effective_to)
                except DeviceJustReassignedError as exc:
                    st.error(f"State changed since this page loaded: {exc} "
                             "Refresh and try again.")
                except ParticipantNotFoundError:
                    st.warning("Already deleted.")
                else:
                    freed = ", ".join(
                        f"{d['device_type']} {d['device_id']}"
                        for d in result["devices"]) or "no devices"
                    site_note = (f"; ended site {result['site']} coverage"
                                if result["site"] else "")
                    st.success(f"Unassigned. Freed: {freed}{site_note}.")
                    st.rerun()

    st.divider()
    st.subheader("Delete a participant")
    choice = st.selectbox("Participant", list(by_pid), index=None,
                          key="delete_pick")
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
