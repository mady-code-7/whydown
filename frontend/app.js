"use strict";

const API_URL =
  location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://127.0.0.1:5000/api/public/check"
    : "https://whydown.onrender.com/api/public/check";

const TIMEOUT_MS = 90000;
const UP_RESULTS = ["OK", "SLOW", "DNS_SLOW"];

const form = document.getElementById("check-form");
const input = document.getElementById("url-input");
const button = document.getElementById("check-button");
const statusLine = document.getElementById("status");
const resultBox = document.getElementById("result");
const resultTitle = document.getElementById("result-title");
const resultNote = document.getElementById("result-note");
const resultError = document.getElementById("result-error");
const stageRows = document.getElementById("stage-rows");
const resultMeta = document.getElementById("result-meta");

function formatMs(value) {
  return typeof value === "number" ? Math.round(value) + " ms" : "-";
}

function addRow(label, value) {
  const row = document.createElement("tr");
  const name = document.createElement("td");
  const time = document.createElement("td");
  name.textContent = label;
  time.textContent = value;
  row.append(name, time);
  stageRows.append(row);
}

function showMessage(text) {
  resultBox.hidden = true;
  statusLine.textContent = text;
}

function showResult(data) {
  const result = data.result || {};
  const isUp = UP_RESULTS.includes(result.result);

  statusLine.textContent = "";
  resultTitle.textContent = data.normalised_url + " - " + result.result;
  resultTitle.className = isUp ? "ok" : "bad";

  if (data.status === "cached") {
    const when = data.checked_at ? new Date(data.checked_at).toLocaleString() : "earlier";
    resultNote.textContent = "Already checked. This is the saved result from " + when + ".";
  } else {
    resultNote.textContent = "";
  }

  if (result.error_message || result.failed_stage) {
    resultError.textContent =
      "Failed at stage: " + (result.failed_stage || "unknown") + ". " + (result.error_message || "");
  } else {
    resultError.textContent = "";
  }

  stageRows.replaceChildren();
  addRow("DNS", formatMs(result.dns_ms));
  addRow("TCP", formatMs(result.tcp_ms));
  addRow("TLS", formatMs(result.tls_ms));
  addRow("First byte", formatMs(result.ttfb_ms));
  addRow("Total", formatMs(result.total_ms));

  const flags = [];
  if (result.cert_expired) {
    flags.push("expired");
  }
  if (result.cert_expiring_soon) {
    flags.push("expiring soon");
  }
  if (result.cert_invalid) {
    flags.push("invalid");
  }
  const code = typeof result.status_code === "number" ? String(result.status_code) : "none";
  resultMeta.textContent =
    "HTTP status: " + code + (flags.length ? ". Certificate: " + flags.join(", ") : "");
  resultBox.hidden = false;
}

async function runCheck(event) {
  event.preventDefault();
  const address = input.value.trim();
  if (!address) {
    showMessage("Enter a website address.");
    return;
  }

  button.disabled = true;
  showMessage("Checking... the first check can take up to a minute while the server wakes up.");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: address }),
      signal: controller.signal,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (data && (data.status === "ok" || data.status === "cached")) {
      showResult(data);
    } else if (data && (data.message || data.error)) {
      showMessage(data.message || data.error);
    } else {
      showMessage("Something went wrong. Try again in a minute.");
    }
  } catch (error) {
    showMessage("The server did not answer. Try again in a minute.");
  } finally {
    clearTimeout(timer);
    button.disabled = false;
  }
}

form.addEventListener("submit", runCheck);