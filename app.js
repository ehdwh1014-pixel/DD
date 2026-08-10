/*
 * Production connection:
 * set API_BASE to the local industrial-PC gateway address, for example:
 * const API_BASE = "http://127.0.0.1:8080/api";
 * The gateway, not this browser, must authenticate and write physical AO/DO.
 */
const API_BASE = "";
const state = {
  valveMode: localStorage.getItem("valveMode") || "auto",
  waterPumpRunning: localStorage.getItem("waterPumpRunning") === "true",
  waterPumpSeconds: Number(localStorage.getItem("waterPumpSeconds") || 0),
  ngVoltage: Number(localStorage.getItem("ngVoltage") || 0),
  events: JSON.parse(localStorage.getItem("controlEvents") || "[]"),
};

const $ = (id) => document.getElementById(id);
const dialog = $("confirmDialog");
let pendingAction;

function save() {
  localStorage.setItem("valveMode", state.valveMode);
  localStorage.setItem("waterPumpRunning", state.waterPumpRunning);
  localStorage.setItem("waterPumpSeconds", state.waterPumpSeconds);
  localStorage.setItem("ngVoltage", state.ngVoltage);
  localStorage.setItem("controlEvents", JSON.stringify(state.events.slice(0, 100)));
}

function showError(message) {
  const alert = $("alert");
  alert.textContent = message;
  alert.hidden = false;
}

async function sendCommand(endpoint, payload) {
  if (!API_BASE) return { simulation: true };
  const response = await fetch(`${API_BASE}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`게이트웨이 응답 오류 (${response.status})`);
  return response.json();
}

function log(message) {
  state.events.unshift(`${new Date().toLocaleString("ko-KR")} — ${message}`);
  save();
  renderLog();
}

function renderLog() {
  $("eventLog").replaceChildren(...state.events.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
}

function render() {
  const isManual = state.valveMode === "manual";
  $("valveMode").textContent = isManual ? "수동" : "자동";
  $("valveMode").className = `pill ${isManual ? "manual" : "auto"}`;
  $("manualValvePanel").hidden = !isManual;
  $("waterPumpState").textContent = state.waterPumpRunning ? "운전 중" : "정지";
  $("waterPumpState").className = `pill ${state.waterPumpRunning ? "running" : ""}`;
  $("waterPumpToggle").textContent = state.waterPumpRunning ? "물 펌프 정지" : "물 펌프 시작";
  $("waterPumpHours").textContent = (state.waterPumpSeconds / 3600).toFixed(2);
  $("ngVoltage").value = state.ngVoltage;
  $("ngVoltageOutput").textContent = `${state.ngVoltage.toFixed(2)} V`;
  renderLog();
}

function confirm(title, message, action) {
  $("dialogTitle").textContent = title;
  $("dialogMessage").textContent = message;
  pendingAction = action;
  dialog.showModal();
}

dialog.addEventListener("close", async () => {
  if (dialog.returnValue !== "confirm" || !pendingAction) return;
  try {
    await pendingAction();
  } catch (error) {
    showError(`명령이 실행되지 않았습니다: ${error.message}`);
  } finally {
    pendingAction = undefined;
  }
});

$("autoValve").addEventListener("click", async () => {
  await sendCommand("/valve/mode", { mode: "auto" });
  state.valveMode = "auto";
  save();
  log("전동 볼밸브를 레벨 센서 자동 제어로 전환");
  render();
});

$("manualValve").addEventListener("click", () => {
  confirm("수동 밸브 제어 전환", "자동 레벨 제어가 중지됩니다. 수동 제어로 전환할까요?", async () => {
    await sendCommand("/valve/mode", { mode: "manual" });
    state.valveMode = "manual";
    save();
    log("전동 볼밸브를 수동 제어로 전환");
    render();
  });
});

document.querySelectorAll("[data-valve-command]").forEach((button) => {
  button.addEventListener("click", () => {
    const command = button.dataset.valveCommand;
    const label = command === "open" ? "열기" : "닫기";
    confirm(`밸브 ${label}`, `DO 출력으로 전동 볼밸브 ${label} 명령을 보냅니다. 실행할까요?`, async () => {
      await sendCommand("/valve/command", { command });
      log(`수동 DO 명령: 전동 볼밸브 ${label}`);
    });
  });
});

$("waterPumpToggle").addEventListener("click", async () => {
  const running = !state.waterPumpRunning;
  await sendCommand("/ao/0", { enabled: running });
  state.waterPumpRunning = running;
  save();
  log(`AO0 물 펌프 ${running ? "시작" : "정지"}`);
  render();
});

$("resetWaterHours").addEventListener("click", () => {
  confirm("누적 시간 초기화", "물 펌프 운전 시간을 0으로 초기화할까요? 이 브라우저의 기록만 초기화됩니다.", async () => {
    state.waterPumpSeconds = 0;
    save();
    log("물 펌프 운전 시간 초기화");
    render();
  });
});

$("ngVoltage").addEventListener("input", (event) => {
  $("ngVoltageOutput").textContent = `${Number(event.target.value).toFixed(2)} V`;
});

$("applyNgVoltage").addEventListener("click", () => {
  const voltage = Number($("ngVoltage").value);
  confirm("AO1 전압 적용", `NG 펌프 AO1에 ${voltage.toFixed(2)} V를 출력합니다. 적용할까요?`, async () => {
    await sendCommand("/ao/1", { voltage });
    state.ngVoltage = voltage;
    save();
    log(`AO1 NG 펌프 전압 적용: ${voltage.toFixed(2)} V`);
    render();
  });
});

$("stopNgPump").addEventListener("click", () => {
  confirm("NG 펌프 정지", "AO1 출력을 0.00 V로 변경합니다. 적용할까요?", async () => {
    await sendCommand("/ao/1", { voltage: 0 });
    state.ngVoltage = 0;
    save();
    log("AO1 NG 펌프 0.00 V 정지");
    render();
  });
});

$("clearLog").addEventListener("click", () => {
  state.events = [];
  save();
  renderLog();
});

setInterval(() => {
  if (!state.waterPumpRunning) return;
  state.waterPumpSeconds += 1;
  save();
  $("waterPumpHours").textContent = (state.waterPumpSeconds / 3600).toFixed(2);
}, 1000);

render();
