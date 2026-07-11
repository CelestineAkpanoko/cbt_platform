"""Streamlit enrollment portal — Fitbit OAuth connection + participant
registration in one flow.

Flow (OAuth must come first: the OAuth redirect reloads the page with
?code=... in the URL, which would wipe any form fields typed beforehand):

  1. Landing (no ?code): welcome copy + "Connect with Fitbit" button.
  2. After Fitbit approval (?code present): exchange the code for tokens
     (once — guarded by session_state), save tokens to S3 exactly as the
     standalone auth portal does today, then reveal the registration form.
  3. Registration form: demographics, user_id + email, mode (research shows
     a Cosinuss pick, production omits it entirely), the Clarity id for the
     work site, and consent. The fitbit_id is AUTO-FILLED from the OAuth
     step — the OAuth id is the same id the pull Lambda lands raw data
     under (fitbit/raw/<fitbit_id>/), so there is no manual Fitbit pick and
     no way to attribute a participant to the wrong Fitbit. The token file
     tokens/{fitbit_id}.json is stamped with the participant_id after
     enrollment.

Device inventory: derived live from the raw sensor bucket — the pull
Lambdas land data under fitbit/raw/<id>/, cosinuss/raw/<id>/ and
clarity/raw/<date>/<clarity_id>_*.json, so the bucket itself is the list
of known devices (see registration_service/s3_inventory.py). Cosinuss and
Clarity dropdowns come from there, each with an "Other (new device)"
free-text fallback for hardware that hasn't uploaded yet. site_id IS the
Clarity device id — no separate location abstraction.

All enrollment business logic lives in registration_service.register()
(fully unit-tested); this file is UI + OAuth + AWS wiring only.

Config resolves from st.secrets first (Streamlit Cloud), then environment
variables (local testing). See .streamlit/secrets.toml.example.
"""

import base64
import datetime
import hashlib
import json
import os
import sys
import urllib.parse

# Make the monorepo's packages importable no matter where this app is
# launched from (repo root, this directory, or Streamlit Cloud, which runs
# the main file from the repo checkout without any PYTHONPATH setup).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in ("shared-lib", "services/assignment-ledger",
           "services/participant-registration"):
    _full = os.path.join(_REPO_ROOT, _p)
    if _full not in sys.path:
        sys.path.insert(0, _full)

import boto3
import requests
import streamlit as st

from cbt_shared.tenancy import ScopedTable
from registration_service import (
    DuplicateSubmissionError,
    RegistrationRequest,
    ValidationError,
    list_wearable_ids,
    register,
    unassigned_devices,
)

# ---------------------------------------------------------------------------
# Config (st.secrets on Streamlit Cloud, env vars locally)
# ---------------------------------------------------------------------------
def conf(key, default=None):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


# Toggle from secrets/env — no code change or redeploy needed to see the
# real Fitbit error text while troubleshooting a live app. Add
# DEBUG_MODE = true to secrets.toml, or unset it (default off) once done;
# do not leave this on in a production deployment (echoes response bodies).
DEBUG_MODE = str(conf("DEBUG_MODE", "false")).strip().lower() in ("1", "true", "yes")

CLIENT_ID = conf("FITBIT_CLIENT_ID")
# MUST equal this app's own public URL and match the Fitbit dev-console
# "Redirect URL" exactly. No default — a stale/wrong default here is
# exactly the class of bug that silently sends users to the wrong app; if
# it isn't set, fail loudly below rather than guess.
REDIRECT_URI = conf("REDIRECT_URI")

_missing = [k for k, v in {
    "FITBIT_CLIENT_ID": CLIENT_ID,
    "REDIRECT_URI": REDIRECT_URI,
}.items() if not v]
if _missing:
    st.error(
        "This app is missing required configuration: "
        f"**{', '.join(_missing)}**.\n\n"
        "Add them under Settings → Secrets in Streamlit Cloud (see "
        "`.streamlit/secrets.toml.example` for the exact keys), then "
        "reboot the app. Nothing else on this page will work until "
        "these are set — Fitbit will reject an authorization request "
        "with a missing/placeholder client_id."
    )
    st.stop()

SCOPES = (
    "activity heartrate location nutrition profile settings sleep social weight "
    "respiratory_rate temperature oxygen_saturation cardio_fitness "
    "electrocardiogram irregular_rhythm_notifications"
)

# Default/fallback org_id — the actual value used for any given
# registration is typed into the form (Step 3), since org_id is per-tenant
# scoping and this portal may serve more than one org over time. This is
# just what pre-fills the field and what the OAuth step (which happens
# before org_id is known) stamps into the token file provisionally.
DEFAULT_ORG_ID = conf("CBT_ORG_ID", "org1")
AWS_REGION = conf("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = conf("S3_BUCKET_NAME", "fitbit-study-tokens-stored")
S3_TOKEN_PREFIX = conf("S3_TOKEN_PREFIX", "fitbit-tokens/")
# The raw sensor bucket doubles as the device inventory for Fitbit/Cosinuss
# (see module docstring) — folder-per-device-id, so prefix listing works.
# Clarity does NOT use this: its raw data is per-minute CSVs (columns like
# datasourceid/sourceid), not a per-device folder — so there is no
# automatic Clarity/site dropdown. The research admin types the Clarity
# device id directly (see Step 3), same as org_id, but it's validated
# against KNOWN_CLARITY_IDS below rather than accepted unchecked.
RAW_BUCKET = conf("RAW_BUCKET", "raw-data-all-sensors-782329476642-us-east-1-an")

# Comma-separated whitelist of valid Clarity device ids for this study
# (currently just one station: DGFVZ0274). A typed site_id that doesn't
# match gets rejected with a clear "contact the research admin" message
# instead of silently misattributing environmental data. Empty/unset =
# validation is skipped entirely (useful if a future org hasn't fixed
# its station roster yet).
KNOWN_CLARITY_IDS = [c.strip() for c in (conf("CLARITY_ID", "") or "").split(",")
                     if c.strip()]

OTHER_OPTION = "Other (new device — type its ID)"


# ---------------------------------------------------------------------------
# AWS clients — explicit keys from secrets if provided, else ambient creds
# ---------------------------------------------------------------------------
_ak = conf("AWS_ACCESS_KEY_ID")
_sk = conf("AWS_SECRET_ACCESS_KEY")
if _ak and _sk:
    _session = boto3.Session(aws_access_key_id=_ak, aws_secret_access_key=_sk,
                             region_name=AWS_REGION)
else:
    _session = boto3.Session(region_name=AWS_REGION)

_dynamodb = _session.resource("dynamodb")
_client = _session.client("dynamodb")
_s3 = _session.client("s3")

# NB: no ScopedTable objects here — they depend on org_id, which is a form
# field entered in Step 3, not a fixed app-wide constant. Built dynamically
# below once the org_id field's current value is known.


@st.cache_data(ttl=300)
def known_cosinuss_ids() -> list[str]:
    return list_wearable_ids(_s3, RAW_BUCKET, "cosinuss")


# ---------------------------------------------------------------------------
# Styling (from the existing Fitbit auth portal)
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
        .title-beige { color:#e4d7b7; font-size:60px; font-weight:800;
                       text-align:center; padding-bottom:10px; }
        .subheader { text-align:center; font-size:34px; font-weight:600;
                     padding-bottom:20px; }
        .normal-text { text-align:center; font-size:22px; line-height:1.5;
                       max-width:900px; margin:auto; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# PKCE helpers (unchanged from the existing portal)
# ---------------------------------------------------------------------------
def generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(64)).decode("utf-8").rstrip("=")


def generate_code_challenge(verifier: str) -> str:
    sha256 = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(sha256).decode("utf-8").rstrip("=")


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


# ---------------------------------------------------------------------------
# STEP 1 / STEP 2 — Fitbit OAuth (exchange happens exactly once)
# ---------------------------------------------------------------------------
params = st.query_params
auth_code = _first(params.get("code"))
returned_state = _first(params.get("state"))

if "fitbit" not in st.session_state:
    if not auth_code:
        # --- Landing page: show the Connect button, then stop. ---
        st.markdown(
            "<h1 class='title-beige'>Welcome to the Heat Stress Research Study!</h1>",
            unsafe_allow_html=True,
        )
        st.markdown("<div class='subheader'>Step 1 — Connect your Fitbit</div>",
                    unsafe_allow_html=True)
        st.markdown(
            "<div class='normal-text'>Securely connect your Fitbit account. "
            "After you log in and approve, we'll continue to enrollment.</div>",
            unsafe_allow_html=True,
        )
        st.info(
            "**Connecting a different Fitbit account than the one already "
            "logged into this browser?** Fitbit will silently reconnect the "
            "same account instead of prompting you to log in again, unless "
            "you first log out at fitbit.com, or open this page in a "
            "private/incognito window.",
            icon="⚠️",
        )
        verifier = generate_code_verifier()
        challenge = generate_code_challenge(verifier)
        # verifier travels in `state` so the callback can recover it even if
        # session_state is lost across the redirect.
        auth_url = (
            "https://www.fitbit.com/oauth2/authorize?response_type=code"
            f"&client_id={CLIENT_ID}"
            f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
            f"&scope={urllib.parse.quote(SCOPES)}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
            f"&state={urllib.parse.quote(verifier)}"
            # Best-effort: some OAuth2 servers honor `prompt=login` to force
            # a fresh login instead of silently reusing the browser's
            # existing session. NOT documented in Fitbit's public OAuth2
            # docs as of this writing — unverified whether Fitbit respects
            # it. Harmless if ignored; the st.info() above is the
            # guaranteed-to-work fallback (logout/incognito).
            "&prompt=login"
        )
        st.markdown(
            f"""<div style="text-align:center; margin-top:25px;">
                <a href="{auth_url}" style="background-color:#4CAF50; color:white;
                   padding:15px 30px; border-radius:10px; font-size:24px;
                   font-weight:bold; text-decoration:none; display:inline-block;">
                   Connect with Fitbit</a></div>""",
            unsafe_allow_html=True,
        )
        st.stop()

    # --- Callback: exchange the code for tokens (only reached once). ---
    code_verifier = returned_state or st.session_state.get("code_verifier")
    if not code_verifier:
        st.error("Something went wrong verifying your connection. Please start over.")
        st.stop()

    resp = requests.post(
        "https://api.fitbit.com/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": auth_code,
            "code_verifier": code_verifier,
            "redirect_uri": REDIRECT_URI,
        },
    )
    if resp.status_code != 200:
        st.error("We couldn't finish connecting to Fitbit. Please try again.")
        if DEBUG_MODE:
            st.write(resp.text)
        st.stop()

    tokens = resp.json()
    encoded_id = tokens.get("user_id")
    if not encoded_id:
        st.error("Fitbit did not return an account id. Please try again.")
        st.stop()

    # Save tokens to S3, same shape/key the pull Lambda reads today:
    # access_token, refresh_token, user_id, expires_at, token_type — do not
    # drop fields the exchange returned. Fitbit reports expires_in
    # (seconds); the stored files carry an absolute expires_at, so compute
    # it here if the exchange didn't include one already.
    token_payload = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "user_id": encoded_id,
        "token_type": tokens.get("token_type", "Bearer"),
    }
    if tokens.get("expires_at"):
        token_payload["expires_at"] = tokens["expires_at"]
    elif tokens.get("expires_in"):
        token_payload["expires_at"] = int(
            datetime.datetime.now(datetime.timezone.utc).timestamp()
            + tokens["expires_in"]
        )
    # enrollment linkage, filled in after the form is submitted (below).
    # org_id is provisional here — the real value is whatever's typed into
    # the org_id field in Step 3, which overwrites this on successful
    # enrollment (see the stamped write further down).
    token_payload["participant_id"] = None
    token_payload["org_id"] = DEFAULT_ORG_ID

    try:
        _s3.put_object(
            Bucket=S3_BUCKET_NAME, Key=f"{S3_TOKEN_PREFIX}{encoded_id}.json",
            Body=json.dumps(token_payload), ContentType="application/json",
        )
    except Exception as e:
        st.error("We couldn't save your Fitbit connection. Please contact the team.")
        if DEBUG_MODE:
            st.write(str(e))
        st.stop()

    st.session_state["fitbit"] = {
        "encoded_id": encoded_id,
        "token_payload": token_payload,
    }
    # fall through to the form below on this same run


# ---------------------------------------------------------------------------
# STEP 3 — Registration form (only reached once Fitbit is connected)
# ---------------------------------------------------------------------------
fitbit = st.session_state["fitbit"]
encoded_id = fitbit["encoded_id"]

st.markdown(
    f"""<div style="text-align:center;"><div style="background-color:#e8f8ee;
       padding:14px 20px; border-radius:12px; display:inline-block; font-size:20px;
       color:#1b7a4e; font-weight:500; margin-bottom:10px;">
       ✔️ Fitbit account connected (ID: {encoded_id})</div></div>""",
    unsafe_allow_html=True,
)
_reconnect_col = st.columns([3, 1])[1]
with _reconnect_col:
    if st.button("🔄 Connect a different Fitbit device", use_container_width=True):
        # Reset to Step 1. Also clear ?code=/?state= from the URL — Fitbit
        # authorization codes are single-use, so leaving the old ones in
        # place would make the rerun immediately try (and fail) to
        # re-exchange an already-consumed code instead of showing the
        # landing page. The previously saved token file in S3 is untouched.
        st.session_state.pop("fitbit", None)
        st.query_params.clear()
        st.rerun()

st.markdown("<div class='subheader'>Step 2 — Participant enrollment</div>",
            unsafe_allow_html=True)

# org_id and mode both live OUTSIDE st.form: org_id determines which
# tenant's tables everything below queries, and widgets inside st.form
# don't trigger a rerun until submit — so both need to be normal widgets
# to keep the Cosinuss pool (and, on submit, the registration itself)
# scoped to the org actually typed in.
org_id_input = st.text_input(
    "Organization ID", value=DEFAULT_ORG_ID,
    help="Tenant scope for this registration. Leave the default unless "
         "you know this study spans multiple orgs.",
).strip() or DEFAULT_ORG_ID

participants = ScopedTable(_dynamodb.Table("Participants"), org_id_input)
devices = ScopedTable(_dynamodb.Table("DeviceAssignments"), org_id_input)
sites = ScopedTable(_dynamodb.Table("SiteAssignments"), org_id_input)

mode = st.radio("Enrollment mode", ["production", "research"], horizontal=True)

# fitbit_id comes straight from the OAuth step — it's the same id the pull
# Lambda lands raw data under (fitbit/raw/<fitbit_id>/). No manual pick,
# no chance of attributing the participant to the wrong Fitbit.
fitbit_id = encoded_id

# Cosinuss pool derived from the raw bucket (5-min cache, folder-per-device
# layout). NB: widgets inside st.form don't re-render until submit, so the
# "Other" text input below is always visible instead of appearing
# conditionally.
cosinuss_pool = unassigned_devices(devices, "cosinuss", known_cosinuss_ids())

with st.form("enroll"):
    st.text_input("Fitbit ID (from the connected account)", value=fitbit_id,
                  disabled=True,
                  help="Captured automatically during the Fitbit connection "
                       "step — this is the ID your raw data is stored under.")
    user_id = st.text_input("User ID (format: user15)")
    email = st.text_input("Email")
    display_name = st.text_input("Display name / anonymized ID")
    sex = st.selectbox("Sex", ["female", "male", "intersex", "prefer not to say"])
    height_in = st.number_input("Height (in)", 36.0, 90.0, 66.0)
    weight_lbs = st.number_input("Weight (lbs)", 60.0, 500.0, 160.0)
    race = st.text_input("Race/ethnicity")

    # site_id IS the Clarity environmental station's device id — typed
    # directly, not derived from S3 (Clarity's raw data is per-minute CSVs,
    # not a per-device folder like Fitbit/Cosinuss). Checked against
    # KNOWN_CLARITY_IDS on submit below rather than accepted unchecked.
    site_id = st.text_input(
        "Clarity device ID (work site's environmental sensor)",
        help=(
            f"Must match a registered station for this study "
            f"({', '.join(KNOWN_CLARITY_IDS)}). Contact the research admin "
            f"if you don't have the correct ID — do not guess."
            if KNOWN_CLARITY_IDS else
            "The Clarity unit currently covering this participant's work "
            "site — this is its datasourceid. Ask the site organizer if "
            "unsure — do not guess."
        ),
    )

    cosinuss_pick = cosinuss_other = None
    if mode == "research":
        cosinuss_pick = st.selectbox(
            "Cosinuss in-ear sensor (devices not currently worn by anyone)",
            cosinuss_pool + [OTHER_OPTION],
        )
        cosinuss_other = st.text_input(
            "If the Cosinuss sensor isn't listed, type its ID here",
        )

    consent = st.checkbox("Participant has given informed consent")
    submitted = st.form_submit_button("Enroll")


def _resolve_pick(pick, other):
    """Typed override wins; otherwise the dropdown pick (unless 'Other')."""
    other = (other or "").strip()
    if other:
        return other
    if pick and pick != OTHER_OPTION:
        return pick
    return ""

if submitted:
    site_id = (site_id or "").strip()
    if KNOWN_CLARITY_IDS and site_id not in KNOWN_CLARITY_IDS:
        st.error(
            f"'{site_id}' isn't a recognized Clarity device for this study. "
            "No registration was created. Please double-check the ID with "
            "the research admin or support team, then try again."
        )
        st.stop()
    cosinuss_id = _resolve_pick(cosinuss_pick, cosinuss_other) or None
    req = RegistrationRequest(
        user_id=(user_id or "").strip(), email=(email or "").strip(),
        display_name=(display_name or "").strip(), sex=sex, height_in=height_in,
        weight_lbs=weight_lbs, race=race, enrollment_mode=mode,
        consent_given=consent, site_id=site_id, fitbit_id=fitbit_id,
        cosinuss_id=cosinuss_id,
        effective_from=datetime.datetime.now(datetime.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    )
    try:
        result = register(_client, participants, devices, sites, req)
    except ValidationError as e:
        st.error(str(e))
    except DuplicateSubmissionError:
        st.warning("This registration was already submitted — no duplicate created.")
    else:
        # Stamp the participant_id + real org_id into the token file so
        # tokens/{fitbit_id}.json is traceable to the enrolled person —
        # rewriting the SAME payload saved at OAuth time (all token fields
        # preserved). org_id was only a provisional default at OAuth time
        # (org_id wasn't known yet); this replaces it with what was
        # actually typed into the form.
        # Best-effort: never fail an otherwise-successful enrollment on it.
        try:
            stamped = {**fitbit["token_payload"],
                       "participant_id": result.participant_id,
                       "org_id": org_id_input}
            _s3.put_object(
                Bucket=S3_BUCKET_NAME, Key=f"{S3_TOKEN_PREFIX}{encoded_id}.json",
                Body=json.dumps(stamped), ContentType="application/json",
            )
        except Exception as e:
            if DEBUG_MODE:
                st.write("linkage write failed:", str(e))

        if result.created_new_participant:
            st.success(f"Enrolled new participant {result.participant_id}")
        else:
            st.success(
                f"Welcome back! Re-enrolled existing participant "
                f"{result.participant_id} (matched on {result.matched_on})."
            )
        st.info(
            "You can close this page. To enroll another participant, reopen "
            "the link fresh (each participant connects their own Fitbit)."
        )
