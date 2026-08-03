"""Backfill Fitbit intraday data the every-minute puller missed.

The production puller (fitbit-pull-to-s3) only ever fetches TODAY and
overwrites in place. If a watch syncs late (evening, or the next morning),
yesterday's fuller data lands in Fitbit's cloud but the puller has already
moved on to the new day and never re-pulls it — so the raw bucket keeps a
partial/empty file even though the cloud has the data. This tool re-pulls
the last N days for each participant and writes them to the raw bucket in
the exact key layout the puller uses, which in turn triggers
heat-stress-predict (S3 ObjectCreated) to (re)generate predictions.

Read-only against Fitbit except for the unavoidable OAuth token rotation
(Fitbit refresh tokens are single-use; a refreshed token is persisted back
to the same S3 key the puller reads, so ingestion keeps working). Writes
to the raw bucket ONLY for days that actually have heart-rate data, so it
can never overwrite a good stored file with an empty one.

Config (client id/secret, bucket names) is read straight from the deployed
puller Lambda's environment — nothing secret lives in this repo. Override
with env vars if needed.

Usage:
    # dry run — report how many HR points the cloud has per participant/day
    python -m ops.backfill_fitbit --days 5
    # actually write the recovered days to the raw bucket (triggers predict)
    python -m ops.backfill_fitbit --days 5 --commit
    # limit to specific participants (fitbit device ids)
    python -m ops.backfill_fitbit --days 5 --participant D58MBD --commit
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

FITBIT_API = "https://api.fitbit.com"
PULLER_FUNCTION = "fitbit-pull-to-s3"

# Same endpoints + output names the puller writes; heart_rate_intraday is
# the one heat-stress-predict strictly requires, the rest are model features.
PARAMS = {
    "heart_rate_intraday": "/1/user/-/activities/heart/date/{date}/1d/1min.json",
    "steps_intraday":      "/1/user/-/activities/steps/date/{date}/1d/1min.json",
    "calories_intraday":   "/1/user/-/activities/calories/date/{date}/1d/1min.json",
    "distance_intraday":   "/1/user/-/activities/distance/date/{date}/1d/1min.json",
}


def _puller_config() -> dict:
    cfg = boto3.client("lambda").get_function_configuration(
        FunctionName=PULLER_FUNCTION)
    return cfg.get("Environment", {}).get("Variables", {})


def _api_get(access_token: str, path: str) -> dict:
    req = urllib.request.Request(
        FITBIT_API + path,
        headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _refresh(client_id: str, client_secret: str, refresh_tok: str) -> dict:
    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_tok}).encode()
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        FITBIT_API + "/oauth2/token", data=data,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _access_token(s3, token_bucket: str, token_prefix: str, dev: str,
                  client_id: str, client_secret: str) -> str:
    """Load the token, refreshing (and persisting the rotation) if expired.
    Persisting back is mandatory — Fitbit invalidates the old refresh token
    on use, so a rotation we don't save would strand the puller."""
    key = f"{token_prefix}{dev}.json"
    tok = json.loads(s3.get_object(Bucket=token_bucket, Key=key)["Body"].read())
    now = int(datetime.now(timezone.utc).timestamp())
    if tok.get("expires_at", 0) <= now + 60:
        new = _refresh(client_id, client_secret, tok["refresh_token"])
        tok["access_token"] = new["access_token"]
        tok["refresh_token"] = new.get("refresh_token", tok["refresh_token"])
        tok["expires_at"] = now + int(new.get("expires_in", 28800))
        s3.put_object(Bucket=token_bucket, Key=key,
                      Body=json.dumps(tok).encode("utf-8"),
                      ContentType="application/json")
    return tok["access_token"]


def _list_participants(s3, token_bucket: str, token_prefix: str) -> list[str]:
    resp = s3.list_objects_v2(Bucket=token_bucket, Prefix=token_prefix)
    out = []
    for obj in resp.get("Contents", []):
        name = obj["Key"][len(token_prefix):]
        if name.endswith(".json") and ".bak" not in name:
            out.append(name[:-len(".json")])
    return out


def _hr_points(data: dict) -> int:
    return len(data.get("activities-heart-intraday", {}).get("dataset", []))


def run(days: int, only: list[str] | None, commit: bool) -> int:
    cfg = _puller_config()
    client_id = os.environ.get("FITBIT_CLIENT_ID") or cfg["FITBIT_CLIENT_ID"]
    client_secret = os.environ.get("FITBIT_CLIENT_SECRET") or cfg["FITBIT_CLIENT_SECRET"]
    raw_bucket = os.environ.get("RAW_BUCKET") or cfg["RAW_DATA_BUCKET"]
    raw_prefix = cfg.get("RAW_DATA_PREFIX", "fitbit/raw/")
    token_bucket = cfg.get("TOKEN_BUCKET", "fitbit-study-tokens-stored")
    token_prefix = cfg.get("TOKEN_PREFIX", "fitbit_tokens/")
    tz_offset = 0  # dates are calendar days; Fitbit keys by the participant's local day

    s3 = boto3.client("s3")
    participants = only or _list_participants(s3, token_bucket, token_prefix)
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

    print("MODE:", "COMMIT (writing recovered days)" if commit
          else "DRY RUN (report only; add --commit to write)")
    print(f"participants={participants} days={dates}\n")
    print(f"{'device':<8} {'date':<12} {'cloud HR pts':>12}  action")

    wrote = 0
    for dev in participants:
        try:
            at = _access_token(s3, token_bucket, token_prefix, dev,
                               client_id, client_secret)
        except Exception as e:
            print(f"{dev:<8} {'-':<12} {'-':>12}  TOKEN ERROR: {e}")
            continue
        for date in dates:
            try:
                hr = _api_get(at, PARAMS["heart_rate_intraday"].format(date=date))
            except urllib.error.HTTPError as e:
                print(f"{dev:<8} {date:<12} {'-':>12}  HTTP {e.code}")
                continue
            pts = _hr_points(hr)
            if pts == 0:
                print(f"{dev:<8} {date:<12} {pts:>12}  skip (no HR in cloud)")
                continue
            if not commit:
                print(f"{dev:<8} {date:<12} {pts:>12}  would recover")
                continue
            # write HR first (the required input), then the activity params
            _put(s3, raw_bucket, raw_prefix, dev, date, "heart_rate_intraday", hr)
            for name, path in PARAMS.items():
                if name == "heart_rate_intraday":
                    continue
                try:
                    data = _api_get(at, path.format(date=date))
                    _put(s3, raw_bucket, raw_prefix, dev, date, name, data)
                except urllib.error.HTTPError:
                    pass  # optional feature; HR alone is enough to predict
            wrote += 1
            print(f"{dev:<8} {date:<12} {pts:>12}  WROTE (triggers predict)")

    print(f"\n{wrote} participant-day(s) {'written' if commit else 'recoverable'}.")
    return 0


def _put(s3, bucket, prefix, dev, date, name, data):
    key = f"{prefix}{dev}/{date}/{name}.json"
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps(data).encode("utf-8"),
                  ContentType="application/json")


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="how many days back (including today) to re-pull")
    ap.add_argument("--participant", action="append", dest="only",
                    help="fitbit device id; repeatable. Default: all tokens")
    ap.add_argument("--commit", action="store_true",
                    help="actually write recovered days; default is dry-run")
    args = ap.parse_args()
    sys.exit(run(args.days, args.only, args.commit))
