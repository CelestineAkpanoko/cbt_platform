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
  4. Once enrolled, an optional "Connect with Google" step (added for the
     Fitbit API sunset migration — see the Google Health API block below).
     This never creates or matches a participant on its own: Google OAuth
     only proves a Google account, not which study participant it belongs
     to, so it's offered only after Fitbit OAuth has already proven the
     fitbit_id this step attaches to. It writes a second, parallel token
     file under google_health_tokens/{fitbit_id}.json — the existing
     tokens/{fitbit_id}.json (Fitbit's own tokens) is never touched.

Identity: the fitbit_id from the OAuth step is a PROVEN identifier (only
the account holder can complete the flow), while user_id and email are
typed and therefore merely claimed. register() enforces that a claimed
identifier can never override a proven one, so a participant cannot be
enrolled onto someone else's Fitbit account — see the
FitbitAccountInUseError branch below and registration_service.service.

Device inventory (see registration_service/s3_inventory.py):
  Cosinuss  receivers from the raw bucket's cosinuss/raw/ prefixes plus the
            puller's state file, minus any currently worn. A real pool.
  Clarity   station ids read from the `datasourceId` column inside a recent
            clarity/raw/<date>/<timestamp>.csv — they are not in the key,
            so they cannot be listed as prefixes. site_id IS the Clarity
            device id; no separate location abstraction.
  Fitbit    NOT a pool. Captured from OAuth, one account per person.
Both dropdowns keep an "Other" free-text fallback for hardware that hasn't
reported yet.

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

# These come from sibling packages resolved through the sys.path setup
# above, NOT from installed distributions — so on Streamlit Cloud they are
# whatever is in its checkout of the repo. A checkout that is partially
# stale (this file updated, the packages not) fails here with a bare
# "cannot import name X", which looks like a code bug and is not one: the
# fix is always to reboot the app, because a *rerun* re-executes the
# script against the existing checkout while only a *reboot* re-clones.
# Catching it turns a traceback nobody can act on into an instruction.
try:
    from cbt_shared.tenancy import ScopedTable
    from registration_service import (
        DuplicateSubmissionError,
        FitbitAccountInUseError,
        RegistrationRequest,
        ValidationError,
        list_clarity_station_ids,
        list_cosinuss_receivers,
        first_fitbit_data_date,
        register,
        unassigned_devices,
    )
    from registration_service.registry import (
        REGISTRY_TABLE_NAME,
        list_clarity_ids as registry_clarity_ids,
        list_cosinuss_ids as registry_cosinuss_ids,
        list_orgs as registry_orgs,
    )
except ImportError as _exc:
    st.error(
        "**This app is running a stale copy of the code.**\n\n"
        f"`{_exc}`\n\n"
        "The enrollment logic lives in sibling packages in this repository "
        "(`registration_service`, `assignment_ledger`, `cbt_shared`). This "
        "error means the deployment picked up a newer version of the app "
        "file than of those packages, so nothing here can run."
    )
    st.info(
        "**Fix:** in the Streamlit Cloud dashboard, open **Manage app → ⋮ → "
        "Reboot app**.\n\n"
        "A *rerun* only re-executes the script against the existing "
        "checkout — only a **reboot** pulls the repository again. If a "
        "reboot does not clear it, use **Clear cache** and reboot, then as "
        "a last resort delete and redeploy the app from the same repo and "
        "branch.",
        icon="🔄",
    )
    st.stop()

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

# --- Google Health API config (added for the Fitbit API sunset migration) --
# Reuses REDIRECT_URI above — this app's single root URL already handles
# the Fitbit callback, and Google's callback lands on the exact same URL.
# The two are told apart by the `state` prefix (GOOGLE_STATE_PREFIX,
# checked where query params are first read, below), never by the query
# param names, since both providers send back `code` and `state`.
#
# Register this SAME REDIRECT_URI on the Google OAuth client too (Google
# Cloud Console -> APIs & Services -> Credentials -> your client ->
# Authorized redirect URIs), alongside the OAuth Playground URI you may
# have added while testing — Console allows more than one.
#
# conf() only reads flat top-level secrets keys (see above), so add these
# as plain top-level entries in secrets.toml, NOT inside a [section]:
#   GOOGLE_CLIENT_ID = "....apps.googleusercontent.com"
#   GOOGLE_CLIENT_SECRET = "..."
GOOGLE_CLIENT_ID = conf("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = conf("GOOGLE_CLIENT_SECRET")
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)
# Parallel to S3_TOKEN_PREFIX below, same bucket — never written into
# fitbit_tokens/, and keyed by the SAME fitbit_id (the Fitbit account's own
# encoded id from the OAuth step), not a separate Google identity. This is
# what lets the Phase 8 puller keep reading everything under one stable key
# per participant regardless of which API actually served the data.
GOOGLE_TOKEN_PREFIX = "google_health_tokens/"
# Marks a `state` value as belonging to the Google flow. Chosen so it can
# never collide with a real Fitbit PKCE code_verifier (generate_code_verifier()
# below never produces a ":" character).
GOOGLE_STATE_PREFIX = "ghoauth:"

# Not a hard stop like the Fitbit config check above — Google reconnection
# is additive, so an unconfigured Google client just hides that section
# rather than breaking enrollment, which must keep working regardless.
_google_configured = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
if not _google_configured and DEBUG_MODE:
    st.caption("Google Health reconnection hidden — GOOGLE_CLIENT_ID / "
              "GOOGLE_CLIENT_SECRET not set in secrets.")

# Preselect only — the org actually used for a registration is picked from
# the Registry-backed dropdown in Step 3 (org_id is per-tenant scoping and
# this portal may serve more than one org). Also what the OAuth step (which
# happens before the dropdown pick exists) stamps into the token file
# provisionally; that stamp is overwritten with the real pick on successful
# enrollment and read by nothing in the meantime, so a stale value here is
# cosmetic, not a data-integrity risk.
#
# NB: org_id used to be a free-text field, and THAT was the actual source
# of past org/participant mismatches — a typo or stray value silently
# created a "shadow tenant" no Lambda reader knew about (see
# ops/add_org.py's docstring). The dropdown below closes that; this
# constant is now just a UI convenience, not the source of truth.
DEFAULT_ORG_ID = conf("CBT_ORG_ID", "org1")
AWS_REGION = conf("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = conf("S3_BUCKET_NAME", "fitbit-study-tokens-stored")
# NOT configurable, on purpose. This must match the pull Lambda's
# TOKEN_PREFIX exactly: a re-registration rotates the Fitbit grant, killing
# the old refresh token, so a writer and reader that disagree about the
# prefix silently end that participant's ingestion.
#
# It was configurable via an S3_TOKEN_PREFIX secret, and that footgun fired
# twice. 2026-07-12: user14's token written to fitbit-tokens/ (hyphen)
# while the puller read fitbit_tokens/ (underscore). 2026-08-02: four newly
# authorized accounts (DDV4XG, DDV99N, DDVJF5, DDVK47) landed in the
# hyphenated prefix again. Both times the code default was correct and a
# deployment secret overrode it — so the default is not the thing to fix.
#
# Any S3_TOKEN_PREFIX value still set in Streamlit Cloud secrets is now
# inert; delete it at your leisure.
S3_TOKEN_PREFIX = "fitbit_tokens/"
# FALLBACK ONLY (see the Registry section below, which is the primary
# source for both dropdowns). RAW_BUCKET/CLARITY_ID matter only if the
# Registry table is empty or briefly unreachable — otherwise the S3
# inference functions below are defined but never actually called.
# Left configurable rather than deleted so the form still works during
# first rollout, before an admin has seeded the Registry.
RAW_BUCKET = conf("RAW_BUCKET", "raw-data-all-sensors-782329476642-us-east-1-an")

# Comma-separated Clarity station ids, unioned with ones derived from the
# bucket. FALLBACK ONLY — add new stations in the admin portal, not here;
# this is not read once the Registry has at least one clarity entry.
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
    """S3-inferred fallback — only called by cosinuss_id_options() below
    when the Registry has no cosinuss entries yet."""
    return list_cosinuss_receivers(_s3, RAW_BUCKET)


@st.cache_data(ttl=300)
def known_clarity_ids() -> list[str]:
    """S3-inferred fallback — only called by clarity_id_options() below
    when the Registry has no clarity entries yet.

    Clarity ids are NOT derivable from prefixes — the station id is the
    `datasourceId` column inside clarity/raw/<date>/<timestamp>.csv — so
    this reads a recent file rather than listing folders.
    """
    derived = []
    try:
        derived = list_clarity_station_ids(_s3, RAW_BUCKET)
    except Exception:
        pass
    return sorted(set(derived) | set(KNOWN_CLARITY_IDS))


# ---------------------------------------------------------------------------
# Admin-managed Registry (preferred source for every dropdown). The admin
# portal (services/admin-portal) curates these lists; the S3-derived
# inventories above remain only as a fallback while the Registry is empty
# (e.g. during first rollout), so the form is never bricked.
# ---------------------------------------------------------------------------
_registry_table = _dynamodb.Table(REGISTRY_TABLE_NAME)


@st.cache_data(ttl=300)
def registry_org_options() -> list[dict]:
    try:
        return registry_orgs(_registry_table)
    except Exception:
        return []


@st.cache_data(ttl=300)
def cosinuss_id_options() -> list[str]:
    try:
        ids = registry_cosinuss_ids(_registry_table)
    except Exception:
        ids = []
    return ids or known_cosinuss_ids()


@st.cache_data(ttl=300)
def clarity_id_options() -> list[str]:
    try:
        ids = registry_clarity_ids(_registry_table)
    except Exception:
        ids = []
    return ids or known_clarity_ids()


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

# --- Google Health API callback (added for the Fitbit API sunset
# migration) — handled first and unconditionally, before the Fitbit block
# below, because it can arrive whether or not a Fitbit session is still
# present, and must never be mistaken for a Fitbit authorization code:
# both providers redirect back to the same REDIRECT_URI with the same
# `code`/`state` param names, so the `state` prefix is what tells them
# apart. ---
if (_google_configured and auth_code and returned_state
       and returned_state.startswith(GOOGLE_STATE_PREFIX)):
    fitbit_ctx = st.session_state.get("fitbit")
    if not fitbit_ctx or not fitbit_ctx.get("enrolled"):
        # Session was lost (new tab, server restart, etc.) between clicking
        # "Connect with Google" and Google's redirect back. Without an
        # established fitbit_id there is nothing safe to attach these
        # tokens to — fail loudly rather than guess.
        st.error(
            "Your session was lost before this Google connection could be "
            "linked to your enrollment. Please reopen the enrollment link, "
            "reconnect Fitbit and confirm your enrollment, then retry "
            "connecting Google from Step 3."
        )
        st.stop()

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": auth_code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if resp.status_code != 200:
        st.error("We couldn't finish connecting to Google. Please try again.")
        if DEBUG_MODE:
            st.write(resp.text)
    else:
        gtokens = resp.json()
        if not gtokens.get("refresh_token"):
            # Missing when Google silently reuses an existing grant instead
            # of showing the consent screen — happens on a retry without a
            # fresh approval. A token set with no refresh_token is useless
            # to the Phase 8 puller (it dies in ~1 hour), so this is
            # treated as a failure, not a partial success.
            st.error(
                "Google didn't return a long-lived connection (no refresh "
                "token). This usually happens on a retry. Please revoke "
                "access at [myaccount.google.com/permissions]"
                "(https://myaccount.google.com/permissions) under "
                "'Heat Stress Research Study', then connect again."
            )
        else:
            google_payload = {
                "access_token": gtokens["access_token"],
                "refresh_token": gtokens["refresh_token"],
                "token_type": gtokens.get("token_type", "Bearer"),
                "expires_at": int(
                    datetime.datetime.now(datetime.timezone.utc).timestamp()
                    + gtokens.get("expires_in", 3600)
                ),
                # Linkage back to the already-proven identity — never
                # derived from anything Google told us.
                "fitbit_id": fitbit_ctx["encoded_id"],
                "participant_id": fitbit_ctx.get("participant_id"),
                "org_id": fitbit_ctx.get("org_id"),
            }
            try:
                _s3.put_object(
                    Bucket=S3_BUCKET_NAME,
                    Key=f"{GOOGLE_TOKEN_PREFIX}{fitbit_ctx['encoded_id']}.json",
                    Body=json.dumps(google_payload),
                    ContentType="application/json",
                )
            except Exception as e:
                st.error("We couldn't save your Google connection. Please "
                        "contact the team.")
                if DEBUG_MODE:
                    st.write(str(e))
            else:
                st.session_state["fitbit"]["google_connected"] = True
                st.success("Google Health connected — your data will keep "
                           "flowing after Fitbit's API shuts down.")
    # Drop ?code=/?state= so a rerun doesn't try to re-exchange the
    # already-consumed (single-use) Google authorization code — same
    # reasoning as the Fitbit "Connect a different device" handler below.
    st.query_params.clear()
    # Deliberately NOT st.stop() here on the success path: execution falls
    # through to the existing Fitbit session_state (already present) and
    # renders Step 3 normally below, now showing the Google-connected state.

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
        # Re-enabled deliberately: this is the primary defence against
        # enrolling one participant onto another's Fitbit account. The
        # server-side guardrail (FitbitAccountInUseError) catches it either
        # way, but catching it here costs nobody a failed registration.
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

    # Capture the previous token file's timestamp BEFORE overwriting it —
    # for a returning participant it records an earlier authorization of
    # this same account, and enrollment is backdated to the earliest
    # evidence of the account (see the submit handler). Best-effort.
    prior_token_at = None
    try:
        head = _s3.head_object(Bucket=S3_BUCKET_NAME,
                               Key=f"{S3_TOKEN_PREFIX}{encoded_id}.json")
        prior_token_at = head["LastModified"].astimezone(
            datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        pass  # first-ever authorization — no prior file

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
        "prior_token_at": prior_token_at,
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
# Org comes from the admin-managed Registry (name + location shown so a
# participant picks the site they actually work at). Free text only as a
# fallback while the Registry has no orgs yet.
_org_options = registry_org_options()
if _org_options:
    _org_labels = {
        f"{o.get('name') or o['org_id']}"
        + (f" — {o['location']}" if o.get("location") else "")
        + f" ({o['org_id']})": o["org_id"]
        for o in _org_options
    }
    _default_idx = next(
        (i for i, v in enumerate(_org_labels.values()) if v == DEFAULT_ORG_ID), 0)
    org_id_input = _org_labels[st.selectbox(
        "Organization", list(_org_labels), index=_default_idx,
        help="Your organization / study site. Ask the research admin if "
             "you are unsure.")]
else:
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

# Cosinuss pool: admin-managed Registry ids (S3-derived fallback while the
# Registry is empty), minus devices currently worn by someone in this org.
cosinuss_pool = unassigned_devices(devices, "cosinuss", cosinuss_id_options())

with st.form("enroll"):
    st.text_input("Fitbit ID (from the connected account)", value=fitbit_id,
                  disabled=True,
                  help="Captured automatically during the Fitbit connection "
                       "step — this is the ID your raw data is stored under.")
    user_id = st.text_input("User ID (format: user15)")
    email = st.text_input("Email")
    display_name = st.text_input("Display name / anonymized ID")
    sex = st.selectbox("Sex", ["female", "male", "intersex", "prefer not to say"])
    age = st.number_input(
        "Age (years)", 18, 100, 30,
        help="Used for the NIOSH heart-rate safety limit (180 − age).")
    height_in = st.number_input("Height (in)", 36.0, 90.0, 66.0)
    weight_lbs = st.number_input("Weight (lbs)", 60.0, 500.0, 160.0)
    race = st.text_input("Race/ethnicity")

    # site_id IS the Clarity environmental station's device id. Now a
    # dropdown of stations actually reporting into the raw bucket (read
    # out of the datasourceId column — Clarity has no per-device folder to
    # list), with a free-text fallback for a unit that is installed but
    # hasn't reported yet.
    clarity_pool = clarity_id_options()
    site_pick = st.selectbox(
        "Clarity device ID (work site's environmental sensor)",
        clarity_pool,
        help="Stations registered for this study. Pick the unit covering "
             "this participant's work site — ask the site organizer if "
             "unsure, do not guess. A missing station must be added by the "
             "research admin (admin portal).",
    ) if clarity_pool else None
    site_other = ""

    cosinuss_pick = cosinuss_other = None
    if mode == "research":
        cosinuss_pick = st.selectbox(
            "Cosinuss in-ear sensor (devices not currently worn by anyone)",
            cosinuss_pool,
            help="A missing sensor must be added by the research admin "
                 "(admin portal).",
        ) if cosinuss_pool else None

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
    site_id = _resolve_pick(site_pick, site_other).strip()
    if not site_id:
        st.error("A Clarity station is required — pick one from the list. "
                 "If none are listed, ask the research admin to add the "
                 "station in the admin portal.")
        st.stop()
    known_stations = clarity_id_options()
    if known_stations and site_id not in known_stations:
        st.error(
            f"'{site_id}' isn't a recognized Clarity station for this study "
            f"(known: {', '.join(known_stations)}). No registration was "
            "created. Please double-check the ID with the research admin or "
            "support team, then try again."
        )
        st.stop()
    cosinuss_id = _resolve_pick(cosinuss_pick, cosinuss_other) or None

    # BACKDATE enrollment to when this Fitbit account actually joined the
    # study, not the moment this form was submitted. After the ledger wipe,
    # everyone re-registers — but their raw data (append-only, survived the
    # wipe) and calibration windows must anchor to the original sign-up.
    # Earliest evidence wins: first raw-data day under
    # fitbit/raw/<fitbit_id>/, or the previous token file's timestamp
    # (captured before this session overwrote it). Genuinely new
    # participants have neither and enroll as of now.
    now_iso = (datetime.datetime.now(datetime.timezone.utc)
               .isoformat().replace("+00:00", "Z"))
    candidates = [now_iso]
    try:
        first_data = first_fitbit_data_date(_s3, RAW_BUCKET, fitbit_id)
        if first_data:
            candidates.append(first_data)
    except Exception:
        pass  # backdating is best-effort; never block an enrollment on it
    if fitbit.get("prior_token_at"):
        candidates.append(fitbit["prior_token_at"])
    effective_from = min(candidates)

    req = RegistrationRequest(
        user_id=(user_id or "").strip(), email=(email or "").strip(),
        display_name=(display_name or "").strip(), sex=sex, age=int(age),
        height_in=height_in,
        weight_lbs=weight_lbs, race=race, enrollment_mode=mode,
        consent_given=consent, site_id=site_id, fitbit_id=fitbit_id,
        cosinuss_id=cosinuss_id,
        effective_from=effective_from,
    )
    try:
        result = register(_client, participants, devices, sites, req)
    except FitbitAccountInUseError as e:
        # The connected Fitbit account belongs to someone else. Almost
        # always the browser silently re-approved a previous participant's
        # Fitbit session rather than anyone acting in bad faith — so lead
        # with the fix, not an accusation. Nothing was written.
        st.error(f"**We did not save this registration.** {e}")
        st.info(
            "**How to connect the right account:** log out at "
            "[fitbit.com](https://www.fitbit.com/logout) *or* reopen this "
            "page in a private/incognito window, then use "
            "**Connect a different Fitbit device** above. Fitbit re-approves "
            "an already-signed-in account without prompting for a login, "
            "which is how the wrong one gets connected.",
            icon="🔑",
        )
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

        # Record enrollment + identity onto the session so the Google
        # reconnection section below (and a future rerun, e.g. after
        # returning from Google's consent screen) knows this fitbit_id is
        # now a confirmed participant, not just a connected Fitbit account.
        st.session_state["fitbit"]["enrolled"] = True
        st.session_state["fitbit"]["participant_id"] = result.participant_id
        st.session_state["fitbit"]["org_id"] = org_id_input

        if effective_from != now_iso:
            st.info(
                f"Enrollment date recorded as **{effective_from[:10]}** — "
                "this Fitbit account was already part of the study, so the "
                "original sign-up date is kept (device history and "
                "calibration anchor to it).",
                icon="📅",
            )
        if result.created_new_participant:
            st.success(f"Enrolled new participant {result.participant_id}")
        else:
            st.success(
                f"Welcome back! Re-enrolled existing participant "
                f"{result.participant_id} (matched on {result.matched_on})."
            )
            if result.matched_on and "fitbit_id" in result.matched_on:
                st.caption(
                    "Recognised from the connected Fitbit account — the "
                    "strongest match we have, since only the account holder "
                    "can complete that step."
                )
        st.info(
            "You can close this page. To enroll another participant, reopen "
            "the link fresh (each participant connects their own Fitbit)."
        )


# ---------------------------------------------------------------------------
# Google Health API reconnection (added for the Fitbit API sunset
# migration) — offered only once enrollment above is confirmed, and shown
# on EVERY rerun from then on (not just the run where the form was
# submitted), since clicking the button navigates away to Google and the
# next run starts fresh with `submitted=False`. See the callback handling
# near the top of this file for the other half of this flow.
# ---------------------------------------------------------------------------
if _google_configured and st.session_state.get("fitbit", {}).get("enrolled"):
    st.markdown("<div class='subheader'>Step 3 — Reconnect via Google Health</div>",
               unsafe_allow_html=True)

    if st.session_state["fitbit"].get("google_connected"):
        st.markdown(
            """<div style="text-align:center;"><div style="background-color:#e8f8ee;
               padding:14px 20px; border-radius:12px; display:inline-block;
               font-size:20px; color:#1b7a4e; font-weight:500;">
               ✔️ Google Health connected</div></div>""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='normal-text'>Fitbit's developer API is being "
            "retired. Connecting your Google account keeps your data "
            "flowing afterward — this adds a second, longer-lasting "
            "connection, it does not replace your Fitbit connection "
            "above.</div>",
            unsafe_allow_html=True,
        )
        # Random nonce after the marker — only the GOOGLE_STATE_PREFIX
        # itself is actually checked on return; the nonce just avoids ever
        # sending an identical state value twice.
        gstate = GOOGLE_STATE_PREFIX + base64.urlsafe_b64encode(
            os.urandom(16)).decode("utf-8").rstrip("=")
        google_auth_url = (
            "https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
            f"&client_id={GOOGLE_CLIENT_ID}"
            f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
            f"&scope={urllib.parse.quote(GOOGLE_SCOPES)}"
            # offline + consent: without both, a returning participant can
            # silently get no refresh_token back (see the callback handler's
            # check above) and the puller dies in about an hour unnoticed.
            "&access_type=offline&prompt=consent"
            f"&state={urllib.parse.quote(gstate)}"
        )
        st.markdown(
            f"""<div style="text-align:center; margin-top:15px;">
                <a href="{google_auth_url}" style="background-color:#4285F4;
                   color:white; padding:15px 30px; border-radius:10px;
                   font-size:24px; font-weight:bold; text-decoration:none;
                   display:inline-block;">Connect with Google</a></div>""",
            unsafe_allow_html=True,
        )

