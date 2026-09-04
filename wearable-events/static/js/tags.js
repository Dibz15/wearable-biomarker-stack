// --- Tags tab ---
// --- Duration-tag entry ---
import { api } from "./core.js";

// Simple, stateless: tap the button, pick or type a duration, log
// immediately with that duration_min. No running timer, nothing that
// can be lost by closing the tab mid-activity.
const DURATION_QUICKPICKS = [5, 10, 15, 30, 60, 90];

export async function loadTagButtons() {
  const container = document.getElementById("tag-buttons");
  try {
    const defs = await api("/tag_definitions");
    if (!defs.length) {
      container.innerHTML = '<p class="muted">No tag buttons configured yet.</p>';
      return;
    }
    container.innerHTML = "";
    defs.forEach(def => {
      const btn = document.createElement("button");
      btn.className = "tag-btn";
      btn.textContent = def.label;
      if (def.is_duration) {
        btn.addEventListener("click", () => openDurationPicker(def.tag, def.label));
      } else {
        btn.addEventListener("click", () => logTag(def.tag, btn));
      }
      container.appendChild(btn);
    });
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load: ${e.message}</p>`;
  }
}

async function logTag(tag, btnEl) {
  const status = document.getElementById("tags-status");
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag] }) });
    if (btnEl) {
      btnEl.classList.add("flash");
      setTimeout(() => btnEl.classList.remove("flash"), 300);
    }
    status.textContent = `Logged "${tag}"`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

function closeDurationPicker() {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "none";
  picker.innerHTML = "";
}

function openDurationPicker(tag, label) {
  const picker = document.getElementById("duration-picker");
  picker.style.display = "block";
  picker.innerHTML = `
    <p class="section-label duration-picker-title">${label} - how long?</p>
    <div class="duration-quickpicks">
      ${DURATION_QUICKPICKS.map(m => `<button class="small-btn duration-quickpick" data-min="${m}">${m}m</button>`).join("")}
    </div>
    <div class="duration-custom-row">
      <input type="number" min="1" id="duration-custom-input" placeholder="Custom minutes">
      <button class="small-btn" id="duration-confirm-btn">Log</button>
      <button class="small-btn" id="duration-cancel-btn">Cancel</button>
    </div>
  `;

  picker.querySelectorAll(".duration-quickpick").forEach(qb => {
    qb.addEventListener("click", () => logDurationTag(tag, label, parseInt(qb.dataset.min, 10)));
  });

  const customInput = document.getElementById("duration-custom-input");
  document.getElementById("duration-confirm-btn").addEventListener("click", () => {
    const minutes = parseInt(customInput.value, 10);
    if (!minutes || minutes <= 0) {
      customInput.focus();
      return;
    }
    logDurationTag(tag, label, minutes);
  });
  customInput.addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("duration-confirm-btn").click();
  });

  document.getElementById("duration-cancel-btn").addEventListener("click", closeDurationPicker);

  customInput.focus();
}

async function logDurationTag(tag, label, minutes) {
  const status = document.getElementById("tags-status");
  status.textContent = `Logging "${label}"...`;
  try {
    await api("/events", { method: "POST", body: JSON.stringify({ tags: [tag], duration_min: minutes }) });
    status.textContent = `Logged "${label}" for ${minutes}min`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    closeDurationPicker();
  }
}

document.getElementById("custom-tag-submit").addEventListener("click", () => {
  const input = document.getElementById("custom-tag-input");
  const tag = input.value.trim();
  if (!tag) return;
  logTag(tag, null);
  input.value = "";
});