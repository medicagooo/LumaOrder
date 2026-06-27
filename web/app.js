const state = {
  currentJobId: "",
  pollTimer: 0,
  lastDryRunOk: false,
  lastConfigKey: "",
};

const $ = (id) => document.getElementById(id);

function configKey() {
  return JSON.stringify(readForm(false));
}

function readForm(includeMode, mode = "dry-run") {
  const excludeValue = $("excludeDirs").value.trim();
  const payload = {
    root: $("rootPath").value.trim(),
    output: $("outputPath").value.trim(),
    threshold: $("threshold").value.trim() || "auto",
    excludeDirs: excludeValue ? excludeValue.split(/[,;]+/).map((item) => item.trim()).filter(Boolean) : [],
    prefixWidth: Number($("prefixWidth").value || 4),
    stripExistingPrefix: $("stripPrefix").checked,
    contactSheets: $("contactSheets").value.trim(),
  };
  if (includeMode) {
    payload.mode = mode;
  }
  return payload;
}

function setStatus(text, tone = "") {
  const element = $("serverStatus");
  element.textContent = text;
  element.dataset.tone = tone;
}

function updateSummary(summary = {}) {
  $("directories").textContent = summary.directories ?? 0;
  $("planned").textContent = summary.planned ?? 0;
  $("renamed").textContent = summary.renamed ?? 0;
  $("conflicts").textContent = summary.conflicts ?? 0;
}

function renderLog(snapshot) {
  const lines = snapshot.progress || [];
  const output = [];
  output.push(`status: ${snapshot.status}`);
  if (snapshot.error) {
    output.push(`error: ${snapshot.error}`);
  }
  output.push(...lines);
  $("log").textContent = output.join("\n");
}

function renderArtifacts(artifacts) {
  const container = $("artifacts");
  container.innerHTML = "";
  if (!artifacts) {
    return;
  }
  if (artifacts.csv) {
    const item = document.createElement("div");
    item.textContent = `CSV: ${artifacts.csv}`;
    container.appendChild(item);
  }
  for (const sheet of artifacts.contactSheets || []) {
    const item = document.createElement("div");
    item.textContent = `Sheet: ${sheet}`;
    container.appendChild(item);
  }
}

async function postJob(mode) {
  const payload = readForm(true, mode);
  state.currentJobId = "";
  state.lastDryRunOk = false;
  $("applyButton").disabled = true;
  $("jobId").textContent = "";
  renderArtifacts(null);
  updateSummary({});
  $("log").textContent = "";
  setStatus(mode === "apply" ? "Applying" : "Dry Run", "active");

  const response = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const created = await response.json();
  if (!response.ok) {
    throw new Error(created.error || "request failed");
  }
  state.currentJobId = created.id;
  $("jobId").textContent = created.id.slice(0, 12);
  pollJob(mode);
}

async function pollJob(mode) {
  clearTimeout(state.pollTimer);
  if (!state.currentJobId) {
    return;
  }
  const response = await fetch(`/api/jobs/${state.currentJobId}`);
  const snapshot = await response.json();
  updateSummary(snapshot.summary);
  renderLog(snapshot);

  if (snapshot.status === "completed") {
    const artifactResponse = await fetch(`/api/jobs/${state.currentJobId}/artifacts`);
    renderArtifacts(await artifactResponse.json());
    setStatus("Completed", "ok");
    if (mode === "dry-run" && Number(snapshot.summary.conflicts || 0) === 0) {
      state.lastDryRunOk = true;
      state.lastConfigKey = configKey();
      $("applyButton").disabled = false;
    }
    return;
  }
  if (snapshot.status === "failed") {
    setStatus("Failed", "bad");
    return;
  }

  state.pollTimer = window.setTimeout(() => pollJob(mode), 650);
}

async function loadExamples() {
  const response = await fetch("/examples");
  const payload = await response.json();
  const examples = payload.examples || [];
  $("exampleCount").textContent = String(examples.length);
  const container = $("examples");
  container.innerHTML = "";
  for (const example of examples) {
    const card = document.createElement("a");
    card.className = "example-card";
    card.href = example.url;
    card.target = "_blank";
    card.rel = "noreferrer";
    const img = document.createElement("img");
    img.src = example.url;
    img.alt = example.name;
    const label = document.createElement("span");
    label.textContent = example.name;
    card.append(img, label);
    container.appendChild(card);
  }
}

function bindFormInvalidation() {
  for (const element of document.querySelectorAll("input")) {
    element.addEventListener("input", () => {
      if (state.lastConfigKey !== configKey()) {
        state.lastDryRunOk = false;
        $("applyButton").disabled = true;
      }
    });
  }
}

$("dryRunButton").addEventListener("click", () => {
  postJob("dry-run").catch((error) => {
    setStatus("Failed", "bad");
    $("log").textContent = error.message;
  });
});

$("applyButton").addEventListener("click", () => {
  if (!state.lastDryRunOk || state.lastConfigKey !== configKey()) {
    $("applyButton").disabled = true;
    return;
  }
  postJob("apply").catch((error) => {
    setStatus("Failed", "bad");
    $("log").textContent = error.message;
  });
});

$("refreshExamples").addEventListener("click", () => {
  loadExamples().catch((error) => {
    $("examples").textContent = error.message;
  });
});

bindFormInvalidation();
loadExamples();
