/**
 * Companion (runs on the phone): the only network-capable half of the
 * Fitbit app. Polls the Prediction API and relays the raw payload to the
 * watch — NO alert logic here; per the documented client responsibilities
 * the watch computes the level itself from cbt_pred + participant_age
 * using the shared module.
 *
 * Poll interval: 5 minutes. The heat-stress-predict Lambda emits roughly
 * one prediction per 5-minute window, so polling faster only burns phone
 * battery re-fetching an unchanged latest.json; slower adds staleness on
 * top of the pipeline's own latency. Rationale in docs/feedback-layer.md.
 */
import * as messaging from "messaging";
import { settingsStorage } from "settings";

const POLL_MINUTES = 5;
const POLL_MS = POLL_MINUTES * 60 * 1000;

function apiBase() {
  return JSON.parse(settingsStorage.getItem("apiBase") || '""') ||
    "https://REPLACE-WITH-DEPLOYED-API.execute-api.us-east-1.amazonaws.com";
}

function userId() {
  return JSON.parse(settingsStorage.getItem("userId") || '""');
}

function orgId() {
  return JSON.parse(settingsStorage.getItem("orgId") || '""');
}

function send(payload) {
  if (messaging.peerSocket.readyState === messaging.peerSocket.OPEN) {
    messaging.peerSocket.send(payload);
  }
}

async function poll() {
  const uid = userId();
  if (!uid) {
    send({ type: "error", error: "no user_id configured" });
    return;
  }
  try {
    const org = orgId(); // one API deployment serves every organisation
    const res = await fetch(
      `${apiBase()}/prediction?user_id=${encodeURIComponent(uid)}` +
        (org ? `&org_id=${encodeURIComponent(org)}` : "")
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const p = await res.json();
    send({
      type: "prediction",
      timestamp: p.timestamp,
      cbt_pred: p.cbt_pred,
      fitbit_hr: p.fitbit_hr,
      ambient_temp: p.ambient_temp,
      participant_age: p.participant_age,
      baseline_provisional: p.baseline_provisional,
      fetched_at: p.fetched_at,
    });
  } catch (e) {
    send({ type: "error", error: String(e) });
  }
}

messaging.peerSocket.addEventListener("open", poll);
setInterval(poll, POLL_MS);
