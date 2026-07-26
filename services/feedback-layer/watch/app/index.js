/**
 * Watch app (runs on the device). All computation stays in the backend;
 * this app only renders the latest prediction relayed by the companion,
 * computes the alert level locally via the SHARED module (same file the
 * dashboard imports — never a local copy), and drives haptics.
 *
 * Haptics fire regardless of baseline_provisional: a provisional reading
 * still reflects real sensor data — provisional affects confidence in the
 * number, not whether a genuine high reading alerts the wearer.
 */
import * as messaging from "messaging";
import document from "document";
import { vibration } from "haptics";
import {
  alertLevel,
  hapticsFor,
  hrLimitExceeded,
} from "../../shared/alert-logic/alertLogic.js";

const el = (id) => document.getElementById(id);

let lastLevel = null; // fire one-shot haptics only on level change
let lastPrediction = null;

function fireHaptics(band) {
  const spec = band && hapticsFor(band.level);
  if (!spec) return;
  const changed = lastLevel === null || band.level !== lastLevel;
  // One-shot levels (1–3) buzz when the level is newly entered; the
  // continuous level (4, Critical) re-fires on every update while it lasts.
  if (!changed && !spec.continuous) return;
  let fired = 0;
  const fire = () => {
    vibration.start(spec.pattern);
    fired += 1;
    if (fired < spec.repeat) setTimeout(fire, 1500);
  };
  fire();
}

function render(p) {
  const band = alertLevel(p.cbt_pred);
  const num = (v, d) => (v == null ? "--" : Number(v).toFixed(d));

  el("cbt").text = `${num(p.cbt_pred, 1)}°C`;
  el("hr").text = `HR ${num(p.fitbit_hr, 0)}`;
  el("ambient").text = `Amb ${num(p.ambient_temp, 0)}°C`;
  el("level").text = band ? `L${band.level} ${band.name.toUpperCase()}` : "NO DATA";
  el("level-bar").style.fill = band
    ? ["#2e7d4f", "#c9a800", "#d97b00", "#d7411f", "#d7263d"][band.level]
    : "#5a6b7b";

  // HR-limit warning: independent of, and additive to, the CBT level.
  const hrHigh = hrLimitExceeded(p.fitbit_hr, p.participant_age);
  el("hr-warning").style.display = hrHigh === true ? "inline" : "none";

  // Provisional baseline: small dashed-outline "CAL" tag, visually distinct
  // from the solid alert-level bar — informational, not a safety warning.
  el("provisional").style.display =
    p.baseline_provisional === true ? "inline" : "none";

  el("age-since").text = ago(p.timestamp);
  el("conn").text = "●"; // connected: fresh payload just arrived
  el("conn").style.fill = "#2e7d4f";

  fireHaptics(band);
  lastLevel = band ? band.level : null;
  lastPrediction = p;
}

function ago(ts) {
  const t = Date.parse(ts);
  if (Number.isNaN(t)) return "";
  const min = Math.round((Date.now() - t) / 60000);
  return min <= 0 ? "just now" : `${min} min ago`;
}

messaging.peerSocket.addEventListener("message", (evt) => {
  if (evt.data.type === "prediction") render(evt.data);
  else if (evt.data.type === "error") {
    el("conn").text = "○";
    el("conn").style.fill = "#d7411f";
  }
});
messaging.peerSocket.addEventListener("close", () => {
  el("conn").text = "○";
  el("conn").style.fill = "#5a6b7b";
});

// Keep "time since last prediction" ticking between polls.
setInterval(() => {
  if (lastPrediction) el("age-since").text = ago(lastPrediction.timestamp);
}, 30000);
