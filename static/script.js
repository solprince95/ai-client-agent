// ═══════════════════════════════════════════════════
// AI CLIENT AGENT — Dashboard logic
// ═══════════════════════════════════════════════════

const els = {
  navLinks:   document.querySelectorAll(".nav-link"),
  panels:     document.querySelectorAll(".tab-panel"),

  licensePill: document.getElementById("license-pill"),
  licenseDot:  document.getElementById("license-dot"),
  licenseText: document.getElementById("license-text"),

  radar:    document.getElementById("radar"),
  btnRun:   document.getElementById("btn-run"),
  btnReplies: document.getElementById("btn-replies"),
  runHint:  document.getElementById("run-hint"),

  statSent:  document.getElementById("stat-sent"),
  statTrial: document.getElementById("stat-trial"),
  statTrialLabel: document.getElementById("stat-trial-label"),
  miniService: document.getElementById("mini-service"),
  miniCity:    document.getElementById("mini-city"),

  terminal: document.getElementById("terminal"),

  setupForm: document.getElementById("setup-form"),
  btnSave:   document.getElementById("btn-save"),
  saveStatus: document.getElementById("save-status"),

  planDot:  document.getElementById("plan-dot"),
  planText: document.getElementById("plan-text"),
  planDetail: document.getElementById("plan-detail"),

  licenseInput: document.getElementById("license-input"),
  btnSaveLicense: document.getElementById("btn-save-license"),
  licenseSaveStatus: document.getElementById("license-save-status"),

  presetSelect: document.getElementById("preset-select"),
};

let currentStatus = null;

// ─────────────────────────────────────────────
// SERVICE PRESETS
// Each preset suggests YOUR_SERVICE, YOUR_ABOUT, and BUSINESS_TYPES
// for a non-technical user. They can edit anything afterwards.
// ─────────────────────────────────────────────
const PRESETS = {
  photographer: {
    service: "Wedding & Event Photography",
    about: "I'm a photographer specialising in weddings and events, capturing the moments that matter most with a candid, modern style.",
    businessTypes: "wedding hall, banquet hall, event planner, marriage garden, bridal studio",
  },
  web_designer: {
    service: "Website Design & Development",
    about: "I design and build modern, mobile-friendly websites that help small businesses look professional and attract more customers online.",
    businessTypes: "small business, shop, restaurant, clinic, hotel, gym, school",
  },
  interior_designer: {
    service: "Interior Design",
    about: "I help homeowners and businesses design beautiful, functional spaces — from concept to final styling.",
    businessTypes: "real estate agency, builder, architect, hotel, showroom",
  },
  social_media: {
    service: "Social Media Management",
    about: "I help businesses grow on Instagram and other platforms with consistent posting, engaging content, and a clear strategy.",
    businessTypes: "restaurant, salon, gym, boutique, cafe, clinic",
  },
  video_editor: {
    service: "Video Editing & Content Creation",
    about: "I edit short-form and long-form videos that help businesses tell their story and stand out on social media.",
    businessTypes: "real estate agency, coaching institute, gym, salon, restaurant",
  },
  accountant: {
    service: "Bookkeeping & Accounting Services",
    about: "I help small businesses keep their books organised, file taxes on time, and stay financially healthy — without the stress.",
    businessTypes: "small business, shop, trader, manufacturer, clinic, agency",
  },
  cybersecurity: {
    service: "Website & API Security Scan",
    about: "I'm a cybersecurity analyst specialising in Website & API Security. I help businesses identify vulnerabilities before hackers do.",
    businessTypes: "small business, shop, store, restaurant, hotel, clinic, school, agency, company, office",
  },
  logo_designer: {
    service: "Logo & Brand Identity Design",
    about: "I design memorable logos and brand identities that help businesses look professional and stand out from the competition.",
    businessTypes: "startup, shop, restaurant, boutique, salon, agency",
  },
};

els.presetSelect.addEventListener("change", () => {
  const key = els.presetSelect.value;
  if (!key || key === "custom" || !PRESETS[key]) return;

  const p = PRESETS[key];
  const form = els.setupForm;

  // Only fill fields that are currently empty, so we never overwrite
  // something the person already typed.
  if (!form.elements["YOUR_SERVICE"].value.trim()) {
    form.elements["YOUR_SERVICE"].value = p.service;
  }
  if (!form.elements["YOUR_ABOUT"].value.trim()) {
    form.elements["YOUR_ABOUT"].value = p.about;
  }
  if (!form.elements["BUSINESS_TYPES"].value.trim()) {
    form.elements["BUSINESS_TYPES"].value = p.businessTypes;
  }
});

// ─────────────────────────────────────────────
// TAB NAVIGATION
// ─────────────────────────────────────────────
els.navLinks.forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    els.navLinks.forEach(b => b.classList.toggle("active", b === btn));
    els.panels.forEach(p => p.classList.toggle("active", p.id === `tab-${tab}`));
  });
});

// ─────────────────────────────────────────────
// STATUS LOADING
// ─────────────────────────────────────────────
async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    currentStatus = data;
    renderStatus(data);
  } catch (e) {
    console.error("status error", e);
  }
}

function renderStatus(data) {
  // ── License pill (sidebar) ──
  const lic = data.license;
  els.licenseDot.className = "dot";
  if (!data.profile_complete) {
    els.licenseDot.classList.add("orange");
    els.licenseText.textContent = "Setup required";
  } else if (lic.kind === "licensed") {
    els.licenseDot.classList.add("green");
    els.licenseText.textContent = `Active · ${lic.days_left}d left`;
  } else if (lic.kind === "trial") {
    els.licenseDot.classList.add("orange");
    els.licenseText.textContent = `Trial · ${lic.days_left}d left`;
  } else if (lic.kind === "expired") {
    els.licenseDot.classList.add("red");
    els.licenseText.textContent = "Trial expired";
  } else {
    els.licenseDot.classList.add("orange");
    els.licenseText.textContent = "Setup required";
  }

  // ── Stats card ──
  els.statSent.textContent = data.stats.total_sent ?? 0;

  if (!data.profile_complete) {
    els.statTrial.textContent = "—";
    els.statTrialLabel.textContent = "Complete setup to start";
  } else if (lic.kind === "licensed") {
    els.statTrial.textContent = `${lic.days_left}d`;
    els.statTrialLabel.textContent = "Days left on your plan";
  } else if (lic.kind === "trial") {
    els.statTrial.textContent = `${lic.days_left}d`;
    els.statTrialLabel.textContent = "Days left on free trial";
  } else {
    els.statTrial.textContent = "0d";
    els.statTrialLabel.textContent = "Trial expired — see Billing";
  }

  els.miniService.textContent = data.config.YOUR_SERVICE || "—";
  els.miniCity.textContent = data.config.TARGET_CITY || "—";

  // ── Run buttons ──
  const canRun = data.profile_complete && lic.allowed && !data.running;
  els.btnRun.disabled = !canRun;
  els.btnReplies.disabled = !canRun;

  if (data.running) {
    els.runHint.textContent = "Agent is running… watch the log below.";
    els.radar.classList.add("active");
  } else {
    els.radar.classList.remove("active");
    if (!data.profile_complete) {
      els.runHint.textContent = "Complete Setup first to get started.";
    } else if (!lic.allowed) {
      els.runHint.textContent = "Your trial has ended. Check Billing to continue.";
    } else {
      els.runHint.textContent = "Ready when you are.";
    }
  }

  // ── Billing tab ──
  els.planDot.className = "dot";
  if (lic.kind === "licensed") {
    els.planDot.classList.add("green");
    els.planText.textContent = "Active subscription";
    els.planDetail.textContent = lic.message;
  } else if (lic.kind === "trial") {
    els.planDot.classList.add("orange");
    els.planText.textContent = "Free trial";
    els.planDetail.textContent = `${lic.days_left} day(s) remaining on your free trial.`;
  } else if (lic.kind === "expired") {
    els.planDot.classList.add("red");
    els.planText.textContent = "Trial expired";
    els.planDetail.textContent = "Pay ₹5,000/month and enter your license key below to continue.";
  } else {
    els.planDot.classList.add("orange");
    els.planText.textContent = "Not set up yet";
    els.planDetail.textContent = "Complete Setup to start your free trial.";
  }

  // ── Populate setup form (only on first load, don't overwrite typing) ──
  if (!els.setupForm.dataset.loaded) {
    for (const [key, val] of Object.entries(data.config)) {
      const input = els.setupForm.elements[key];
      if (!input) continue;
      if (key === "BUSINESS_TYPES" && Array.isArray(val)) {
        input.value = val.join(", ");
      } else {
        input.value = val ?? "";
      }
    }
    els.licenseInput.value = data.config.LICENSE_KEY || "";
    els.setupForm.dataset.loaded = "1";
  }
}

// ─────────────────────────────────────────────
// SAVE PROFILE
// ─────────────────────────────────────────────
els.setupForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(els.setupForm);
  const payload = {};
  for (const [key, val] of formData.entries()) payload[key] = val;

  els.btnSave.disabled = true;
  els.saveStatus.textContent = "Saving…";

  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) {
      els.saveStatus.textContent = "✓ Saved";
      await loadStatus();
    } else {
      els.saveStatus.textContent = "Error saving";
    }
  } catch (e) {
    els.saveStatus.textContent = "Network error";
  } finally {
    els.btnSave.disabled = false;
    setTimeout(() => (els.saveStatus.textContent = ""), 2500);
  }
});

// ─────────────────────────────────────────────
// SAVE LICENSE KEY
// ─────────────────────────────────────────────
els.btnSaveLicense.addEventListener("click", async () => {
  els.btnSaveLicense.disabled = true;
  els.licenseSaveStatus.textContent = "Saving…";
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ LICENSE_KEY: els.licenseInput.value.trim() }),
    });
    const data = await res.json();
    if (data.ok) {
      els.licenseSaveStatus.textContent = "✓ Saved";
      await loadStatus();
    } else {
      els.licenseSaveStatus.textContent = "Error saving";
    }
  } catch (e) {
    els.licenseSaveStatus.textContent = "Network error";
  } finally {
    els.btnSaveLicense.disabled = false;
    setTimeout(() => (els.licenseSaveStatus.textContent = ""), 2500);
  }
});

// ─────────────────────────────────────────────
// TERMINAL LOG HELPERS
// ─────────────────────────────────────────────
function clearTerminal() {
  els.terminal.innerHTML = "";
}

function addLogLine(text) {
  const line = document.createElement("div");
  line.className = "terminal-line";

  if (text.startsWith("✅") || text.includes("complete")) line.classList.add("success");
  else if (text.startsWith("❌")) line.classList.add("error");
  else if (text.startsWith("⚠️") || text.startsWith("⛔")) line.classList.add("warn");

  line.textContent = text;
  els.terminal.appendChild(line);
  els.terminal.scrollTop = els.terminal.scrollHeight;
}

// ─────────────────────────────────────────────
// RUN AGENT / CHECK REPLIES — with live log stream
// ─────────────────────────────────────────────
function startLogStream(onDone) {
  const evtSource = new EventSource("/api/stream");

  evtSource.onmessage = (e) => {
    if (e.data.trim() === "") return; // keepalive
    addLogLine(e.data);
  };

  evtSource.addEventListener("done", () => {
    evtSource.close();
    onDone();
  });

  evtSource.onerror = () => {
    evtSource.close();
    onDone();
  };
}

async function triggerRun(endpoint) {
  clearTerminal();
  addLogLine("Starting…");

  const res = await fetch(endpoint, { method: "POST" });
  const data = await res.json();

  if (!data.ok) {
    addLogLine(`⛔ ${data.message}`);
    return;
  }

  els.btnRun.disabled = true;
  els.btnReplies.disabled = true;
  els.radar.classList.add("active");
  els.runHint.textContent = "Agent is running… watch the log below.";

  startLogStream(async () => {
    els.radar.classList.remove("active");
    await loadStatus();
  });
}

els.btnRun.addEventListener("click", () => triggerRun("/api/run"));
els.btnReplies.addEventListener("click", () => triggerRun("/api/check-replies"));

// ─────────────────────────────────────────────
// INIT
// ─────────────────────────────────────────────
loadStatus();
setInterval(loadStatus, 8000);
