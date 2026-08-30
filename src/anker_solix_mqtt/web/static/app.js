const csrfCookieName = "solix_csrf";

function csrfToken() {
  const prefix = `${csrfCookieName}=`;
  const cookie = document.cookie.split("; ").find((entry) => entry.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
}

function requestId() {
  return `web-${crypto.randomUUID()}`;
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "–";
  if (typeof value === "number") {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value);
  }
  if (typeof value === "boolean") return value ? "On" : "Off";
  return String(value);
}

function applySnapshot(snapshot) {
  document.body.dataset.revision = String(snapshot.revision);
  document.querySelectorAll(`[data-value-device="${CSS.escape(snapshot.device_id)}"]`).forEach((node) => {
    node.textContent = displayValue(snapshot.values[node.dataset.valueKey]);
  });

  const health = document.querySelector(`[data-device-health="${CSS.escape(snapshot.device_id)}"]`);
  if (health) {
    health.classList.toggle("is-online", snapshot.online);
    health.classList.toggle("is-offline", !snapshot.online);
    health.querySelector("[data-health-label]").textContent = snapshot.online ? "Online" : "Offline";
  }

  const freshness = document.querySelector(`[data-freshness="${CSS.escape(snapshot.device_id)}"]`);
  if (freshness) {
    freshness.classList.toggle("is-stale", snapshot.stale);
    const timestamp = new Date(snapshot.timestamp).toLocaleString("en-US");
    freshness.textContent = `Updated ${timestamp}${snapshot.stale ? " · Values are stale" : ""}`;
  }
}

async function refreshDevice(deviceId) {
  const response = await fetch(`/api/v1/devices/${encodeURIComponent(deviceId)}/state`);
  if (response.ok) applySnapshot(await response.json());
}

function setResult(form, state, message) {
  const output = form.querySelector(".control-result");
  output.dataset.state = state;
  output.textContent = message;
}

async function sendControl(form) {
  if (form.dataset.pending === "true") return;
  const input = form.elements.value;
  const value = form.dataset.kind === "switch"
    ? input.checked
    : form.dataset.kind === "number"
      ? Number(input.value)
      : input.value;

  input.disabled = true;
  form.dataset.pending = "true";
  setResult(form, "pending", "Sending...");
  try {
    const response = await fetch(`/api/v1/devices/${encodeURIComponent(form.dataset.device)}/commands`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Solix-CSRF": csrfToken(),
      },
      body: JSON.stringify({
        request_id: requestId(),
        entity_key: form.dataset.entity,
        value,
      }),
    });
    const result = await response.json();
    if (!response.ok || result.status !== "success") {
      throw new Error(result.error || result.detail || "Command failed");
    }
    setResult(form, result.warnings.length ? "warning" : "success", result.warnings[0] || "Applied");
    await refreshDevice(form.dataset.device);
  } catch (error) {
    setResult(form, "error", error.message);
    await refreshDevice(form.dataset.device);
  } finally {
    input.disabled = false;
    delete form.dataset.pending;
    if (form.dataset.kind === "switch") {
      form.querySelector(".switch-value").textContent = input.checked ? "On" : "Off";
    }
  }
}

document.querySelectorAll("[data-control]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendControl(form);
  });
  const input = form.elements.value;
  if (form.dataset.kind === "select" || form.dataset.kind === "switch") {
    input.addEventListener("change", () => sendControl(form));
  } else if (form.dataset.kind === "number") {
    input.addEventListener("change", () => sendControl(form));
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      sendControl(form);
    });
  }
});

const events = new EventSource("/api/v1/events");
events.addEventListener("state", (message) => {
  const event = JSON.parse(message.data);
  applySnapshot(event.data);
});
events.addEventListener("metadata", () => window.location.reload());
events.addEventListener("sync", async () => {
  const response = await fetch("/api/v1/devices");
  if (!response.ok) return;
  const { devices } = await response.json();
  await Promise.all(devices.map((device) => refreshDevice(device.device_id)));
});