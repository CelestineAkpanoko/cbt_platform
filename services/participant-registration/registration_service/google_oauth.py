"""Google Health sign-in helpers for the enrollment portal.

The legacy Fitbit Web API and its OAuth endpoints were decommissioned in
Oct/Nov 2026, so the portal connects participants through Google instead.
Everything here is pure (no Streamlit, no network) so it unit-tests offline:
the portal passes in the HTTP responses and the clock.

Facts this module relies on (Google Health API docs, checked 2026-11-13;
re-check them if sign-in starts failing):

- Standard Google OAuth endpoints: accounts.google.com/o/oauth2/v2/auth and
  oauth2.googleapis.com/token.
- Heart rate is covered by
  https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
  (NOT activity_and_fitness, which covers steps and active minutes).
- A refresh token is only issued with access_type=offline + prompt=consent.
- Google lets the user untick individual permissions on the consent screen,
  so the granted scopes must be checked after the exchange.
- Google does not return a Fitbit-style 6-character id with the token. The
  `users/me/identity` call returns a `legacyUserId` when the account was
  migrated from Fitbit; Google-native accounts have none, so the label is
  chosen by the study team at enrollment.
- Unverified OAuth apps are capped at 100 users, and while the app's publishing
  status is "Testing" refresh tokens expire after 7 days.
"""

import re
import urllib.parse

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
IDENTITY_ENDPOINT = "https://health.googleapis.com/v4/users/me/identity"

HEART_RATE_SCOPE = (
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
)
DEFAULT_SCOPES = (HEART_RATE_SCOPE,)

# Label rules: it becomes an S3 path segment and the ledger's fitbit_id, so keep
# it short, uppercase and free of anything that needs escaping. Existing labels
# in this study are 6 characters (CR8Y24, CSFX5L, DDV4XG).
LABEL_RE = re.compile(r"^[A-Z0-9]{4,12}$")


class GoogleConnectError(Exception):
    """Raised with a message that is safe to show to a participant."""


def parse_scopes(raw):
    """Space- or comma-separated string (or list) -> tuple of scope strings."""
    if not raw:
        return DEFAULT_SCOPES
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = re.split(r"[,\s]+", str(raw))
    out = tuple(s.strip() for s in items if s and s.strip())
    return out or DEFAULT_SCOPES


def build_auth_url(client_id, redirect_uri, scopes, challenge, state):
    """Authorization URL. prompt=select_account+consent forces the account
    chooser (the participant may be signed into several Google accounts) and
    guarantees a refresh token even if they connected before."""
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "select_account consent",
        "include_granted_scopes": "false",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def token_request_body(client_id, client_secret, code, verifier, redirect_uri):
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }


def token_payload_from_response(tokens, now_ts, required_scopes=DEFAULT_SCOPES):
    """Validate Google's token response and build the stored token payload.

    The payload has the shape google-health-pull reads: access_token,
    refresh_token, expires_at, token_type. fitbit_id / participant_id / org_id
    are stamped on later, once the study team has chosen the label.
    """
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise GoogleConnectError("Google did not return an access token. Please try again.")
    if not tokens.get("refresh_token"):
        raise GoogleConnectError(
            "Google did not return a long-lived connection. Please try again and "
            "approve every permission on the Google screen."
        )
    granted = set(str(tokens.get("scope", "")).split())
    missing = [s for s in required_scopes if s not in granted]
    if missing:
        raise GoogleConnectError(
            "The heart-rate permission was not approved. Please start again and "
            "leave every permission box ticked on the Google screen."
        )
    try:
        expires_in = int(tokens.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": int(now_ts) + expires_in,
        "token_type": tokens.get("token_type", "Bearer"),
        "provider": "google_health",
        "scope": " ".join(sorted(granted)),
    }


def legacy_id_from_identity(identity):
    """Pull the 6-character legacy Fitbit id out of a users/me/identity response.
    Returns a normalised label or None (Google-native accounts have none)."""
    if not isinstance(identity, dict):
        return None
    return normalize_label(identity.get("legacyUserId"), strict=False)


def normalize_label(value, strict=True):
    """Uppercase/trim a label. strict=True raises GoogleConnectError when it is
    not usable; strict=False returns None instead."""
    label = (value or "").strip().upper()
    if LABEL_RE.match(label):
        return label
    if strict:
        raise GoogleConnectError(
            "The device label must be 4 to 12 letters or digits (for example CR8Y24)."
        )
    return None


def stamped_payload(token_payload, label, participant_id, org_id):
    """Final token file: the label goes in as fitbit_id, which is the field
    google-health-pull uses to decide where the heart-rate file is written."""
    return {**token_payload, "fitbit_id": label,
            "participant_id": participant_id, "org_id": org_id}
