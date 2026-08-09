"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const lightThemes = ["ocean", "mint", "lilac", "rose", "apricot", "jade"];
let lastLightTheme = "ocean";

function storedValue(key, fallback) {
  try { return window.localStorage.getItem(key) || fallback; } catch { return fallback; }
}

function saveValue(key, value) {
  try { window.localStorage.setItem(key, value); } catch { /* Theme still works for this visit. */ }
}

function applyTheme(theme, persist = true) {
  const selected = theme === "dark" || lightThemes.includes(theme) ? theme : "ocean";
  if (selected !== "dark") {
    lastLightTheme = selected;
    if (persist) saveValue("palworld-light-theme", selected);
  }
  document.documentElement.dataset.theme = selected;
  if (persist) saveValue("palworld-theme", selected);
  const color = selected === "dark" ? "#17191d" : getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#f3f7ff";
  $("#themeColor")?.setAttribute("content", color);
  const darkButton = $("#darkModeButton");
  if (darkButton) {
    const dark = selected === "dark";
    darkButton.classList.toggle("is-active", dark);
    darkButton.textContent = dark ? "☀" : "☾";
    darkButton.title = dark ? "切换到浅色模式" : "切换到深色模式";
    darkButton.setAttribute("aria-label", darkButton.title);
  }
}

lastLightTheme = lightThemes.includes(storedValue("palworld-light-theme", "ocean")) ? storedValue("palworld-light-theme", "ocean") : "ocean";
applyTheme(storedValue("palworld-theme", "ocean"), false);

const state = {
  authenticated: false,
  section: "overview",
  status: null,
  checks: null,
  checksLoading: false,
  activeCheckGroup: null,
  settings: [],
  settingsCategories: [],
  category: "全部",
  settingsSearch: "",
  changes: new Map(),
  backups: [],
  logs: { game: "", panel: "" },
  activeLog: "game",
  chart: [],
  history: null,
  historySource: "server",
  historyRange: "playing",
  historyLoading: false,
  historyLoadedAt: 0,
  statusLoading: false,
  pollTimer: null,
  operationPollTimer: null,
  operationNotified: null,
  operationDismissed: null,
  operationAutoDismiss: null,
  settingsSubmitting: false,
  settingsOperationDismissed: null,
  localSettingsOperation: null,
  modalResolve: null,
};

const pageNames = {
  overview: { title: "服务器总览", subtitle: "状态、实时负载与后台性能历史" },
  checks: { title: "完整检查", subtitle: "按分类查看服务、主机、存档、自动任务与配置状态" },
  control: { title: "常用控制", subtitle: "安全执行保存、备份、更新和启停操作" },
  world: { title: "世界设置", subtitle: "用中文查看并调整当前世界参数" },
  backups: { title: "备份仓库", subtitle: "校验、下载或恢复受管存档" },
  logs: { title: "运行日志", subtitle: "查看近期日志并下载脱敏诊断包" },
};

const kindNames = {
  daily: "每日", weekly: "每周", monthly: "每月", event: "事件",
  update: "更新前", manual: "手动",
};

function formatBytes(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  let amount = Number(value);
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index === 0 ? 0 : digits)} ${units[index]}`;
}

function formatPercent(value, digits = 0) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
    ? `${Number(value).toFixed(digits)}%`
    : "—";
}

function formatNumber(value, digits = 1) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
    ? Number(value).toFixed(digits)
    : "—";
}

function formatDuration(value) {
  if (!Number.isFinite(Number(value))) return "—";
  let seconds = Math.max(0, Math.floor(Number(value)));
  const days = Math.floor(seconds / 86400); seconds %= 86400;
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function formatDate(value, compact = false) {
  if (!value) return "—";
  const systemdLocal = String(value).match(/^\w+\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);
  if (systemdLocal) {
    const [, year, month, day, hour, minute] = systemdLocal;
    return compact ? `${month}/${day} ${hour}:${minute}` : `${year}/${month}/${day} ${hour}:${minute}`;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace(/ UTC$/, "").slice(0, 24);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    ...(compact ? {} : { year: "numeric" }), hour12: false,
  }).format(date);
}

function clamp(value, min = 0, max = 100) {
  return Math.max(min, Math.min(max, Number(value) || 0));
}

async function api(path, options = {}) {
  const request = { credentials: "same-origin", ...options };
  request.headers = { ...(options.headers || {}) };
  if (request.body) request.headers["Content-Type"] = "application/json";
  if ((request.method || "GET") !== "GET") request.headers["X-Palworld-Panel"] = "1";
  const response = await fetch(path, request);
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("登录已失效");
  }
  if (!response.ok || payload?.ok === false) throw new Error(payload?.error || `请求失败（HTTP ${response.status}）`);
  return payload;
}

function toast(title, message = "", isError = false) {
  const element = document.createElement("div");
  element.className = `toast${isError ? " is-error" : ""}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const detail = document.createElement("p");
  detail.textContent = message;
  element.append(strong, detail);
  $("#toastStack").append(element);
  window.setTimeout(() => element.remove(), 5200);
}

function showLogin() {
  state.authenticated = false;
  window.clearInterval(state.pollTimer);
  state.pollTimer = null;
  window.clearTimeout(state.operationPollTimer);
  state.operationPollTimer = null;
  $("#appShell").classList.add("is-hidden");
  $("#loginView").classList.remove("is-hidden");
  $("#loginPassword").value = "";
  window.setTimeout(() => $("#loginPassword").focus(), 50);
}

async function showApp() {
  state.authenticated = true;
  $("#loginView").classList.add("is-hidden");
  $("#appShell").classList.remove("is-hidden");
  navigate("overview");
  await refreshStatus(true);
  Promise.allSettled([loadSettings(), loadBackups(), loadChecks(false), loadPerformanceHistory(false)]);
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(() => {
    refreshStatus(false);
    if (Date.now() - state.historyLoadedAt >= 60_000) loadPerformanceHistory(false);
  }, 5000);
}

async function handleLogin(event) {
  event.preventDefault();
  const button = $("#loginButton");
  const error = $("#loginError");
  button.disabled = true;
  button.textContent = "正在验证…";
  error.textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#loginUsername").value.trim(),
        password: $("#loginPassword").value,
        remember: $("#rememberLogin").checked,
      }),
    });
    $("#loginPassword").value = "";
    await showApp();
  } catch (exception) {
    error.textContent = exception.message;
    $("#loginPassword").select();
  } finally {
    button.disabled = false;
    button.textContent = "进入控制台";
  }
}

async function logout() {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch { /* Session may already be gone. */ }
  showLogin();
}

function navigate(name) {
  if (!pageNames[name]) return;
  state.section = name;
  $$(".page-section").forEach((item) => item.classList.toggle("is-active", item.id === `section-${name}`));
  $$(`[data-section]`).forEach((item) => item.classList.toggle("is-active", item.dataset.section === name));
  $("#pageTitle").textContent = pageNames[name].title;
  $("#pageSubtitle").textContent = pageNames[name].subtitle;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "checks") {
    loadChecks(false);
  }
  if (name === "overview") window.requestAnimationFrame(() => { drawChart(); drawHistoryChart(); });
  if (name === "backups") loadBackups();
  if (name === "world" && !state.settings.length) loadSettings();
  if (name === "logs") loadLogs();
}

function setMeter(element, percent, highIsBad = true) {
  const value = clamp(percent);
  element.style.width = `${value}%`;
  element.classList.toggle("warn", highIsBad && value >= 70 && value < 88);
  element.classList.toggle("danger", highIsBad && value >= 88);
}

function normalizePlayers(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.players)) return value.players;
  return [];
}

function renderPlayers(players, maximum) {
  const list = $("#playerList");
  list.replaceChildren();
  $("#playerCount").textContent = `${players.length} / ${maximum || 2}`;
  if (!players.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state compact";
    empty.innerHTML = "<span>静</span><p>目前无人在线</p>";
    list.append(empty);
    return;
  }
  players.forEach((player) => {
    const name = player.name || player.accountName || player.playerName || "玩家";
    const row = document.createElement("div");
    row.className = "player-row";
    const avatar = document.createElement("span");
    avatar.className = "player-avatar";
    avatar.textContent = String(name).slice(0, 1).toUpperCase();
    const detail = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = name;
    const small = document.createElement("small");
    small.textContent = player.level ? `等级 ${player.level}` : "正在世界中";
    detail.append(strong, small);
    row.append(avatar, detail);
    list.append(row);
  });
}

function renderAutomation(timers) {
  const enabled = timers.filter((item) => item.enabled && item.active).length;
  $("#sideAutomation").textContent = enabled === (timers.length || 6) ? "全部正常" : `${enabled} 项运行`;
  const summary = $("#automationSummary");
  if (summary) summary.textContent = `${enabled} / ${timers.length || 6} 项正常`;
  const grid = $("#automationGrid");
  if (!grid) return;
  grid.replaceChildren();
  timers.forEach((timer) => {
    const item = document.createElement("div");
    item.className = `automation-item${timer.enabled && timer.active ? " is-on" : ""}`;
    const name = document.createElement("span");
    name.textContent = timer.label;
    const next = document.createElement("strong");
    next.textContent = timer.next ? `下次 ${formatDate(timer.next, true)}` : (timer.enabled ? "等待调度" : "未启用");
    item.append(name, next);
    grid.append(item);
  });
}

function renderUpdate(update) {
  const tag = $("#updateTag");
  const detail = $("#updateDetail");
  tag.className = "status-tag";
  if (!update) {
    tag.textContent = "尚未检查";
    detail.textContent = "点击“检查更新”查询 Steam 官方构建。";
    return;
  }
  if (update.up_to_date) {
    tag.textContent = "已是最新";
    tag.classList.add("good");
    detail.textContent = `构建 ${update.installed_build} · 最近检查 ${formatDate(update.checked_at)}`;
  } else {
    tag.textContent = "有可用更新";
    tag.classList.add("warn");
    detail.textContent = `当前 ${update.installed_build}，可更新至 ${update.required_build || "新构建"}。`;
  }
}

function operationElapsedText(operation) {
  const started = new Date(operation.started_at || Date.now()).getTime();
  const finished = operation.finished_at ? new Date(operation.finished_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((finished - started) / 1000));
  const duration = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  if (operation.state === "running") return `已执行 ${duration} · 页面正在自动读取服务器的最新进度`;
  if (operation.state === "success" && Number(operation.result?.changed) === 0) return `总耗时 ${duration} · 已复核目标值，无需写入或重启`;
  if (operation.state === "success" && operation.result?.restarted === false) return `总耗时 ${duration} · 设置已复核，游戏服务保持停止`;
  if (operation.state === "success") return `总耗时 ${duration} · 设置和服务状态均已复核`;
  return `执行到 ${duration}时停止 · 原改动仍保留，可关闭后重试`;
}

function renderSettingsOperation(operation) {
  const panel = $("#settingsOperation");
  const current = operation?.name === "settings" ? operation : state.localSettingsOperation;
  if (!current || state.settingsOperationDismissed === current.id) {
    panel.classList.add("is-hidden");
    syncSettingsControls(operation);
    updateSettingsSavebar();
    return;
  }

  const running = current.state === "running";
  const success = current.state === "success";
  const error = current.state === "error";
  const noChanges = success && Number(current.result?.changed) === 0;
  const deferred = success && Number(current.result?.changed) > 0 && current.result?.restarted === false;
  const count = Number(current.change_count ?? current.result?.changed ?? state.changes.size) || 0;
  const progress = clamp(current.progress ?? (success ? 100 : 4));
  const phase = current.failed_phase || current.phase || "queued";
  const stageByPhase = {
    submitting: 0, queued: 0, validating: 0,
    saving: 1, backup: 1,
    stopping: 2, writing: 2,
    starting: 3,
    health: 4, complete: 4,
    "rollback-stopping": 2, "rollback-writing": 2,
    "rollback-starting": 3, "rollback-health": 4,
  };
  const stage = stageByPhase[phase] ?? Math.min(4, Math.floor(progress / 20));

  panel.classList.remove("is-hidden", "is-running", "is-success", "is-error");
  panel.classList.toggle("is-running", running);
  panel.classList.toggle("is-success", success);
  panel.classList.toggle("is-error", error);
  $("#settingsOperationMark").textContent = success ? "✓" : (error ? "!" : "↻");
  $("#settingsOperationTitle").textContent = running
    ? (count ? `正在应用 ${count} 项世界设置` : "正在应用世界设置")
    : (noChanges ? "世界设置已是目标值，无需重启"
      : (deferred ? "世界设置已保存，等待下次启动生效"
        : (success ? "世界设置已应用并确认生效" : "世界设置没有应用成功")));
  $("#settingsOperationState").textContent = running ? "处理中" : (success ? "已完成" : "需要处理");
  $("#settingsOperationMessage").textContent = current.message || "操作已提交，请勿重复点击。";
  $("#settingsOperationProgress").style.width = `${progress}%`;
  $("#settingsProgressTrack").setAttribute("aria-valuenow", String(progress));
  $$(`[data-settings-stage]`).forEach((item, index) => {
    const skipped = (noChanges && index > 0) || (deferred && index > 2);
    item.classList.remove("is-current", "is-complete", "is-error", "is-skipped");
    item.classList.toggle("is-skipped", skipped);
    item.classList.toggle("is-complete", !skipped && (success || index < stage));
    item.classList.toggle("is-current", running && index === stage);
    item.classList.toggle("is-error", error && index === stage);
    item.querySelector("span").textContent = skipped ? "—" : (success || index < stage ? "✓" : (error && index === stage ? "!" : String(index + 1)));
  });
  $("#settingsOperationElapsed").textContent = operationElapsedText(current);
  $("#dismissSettingsOperation").classList.toggle("is-hidden", running);
  state.settingsSubmitting = Boolean(current.local && running);
  syncSettingsControls(current);
  updateSettingsSavebar();
}

function stopOperationPolling() {
  window.clearTimeout(state.operationPollTimer);
  state.operationPollTimer = null;
}

function scheduleOperationPoll() {
  if (!state.authenticated || state.operationPollTimer) return;
  state.operationPollTimer = window.setTimeout(async () => {
    state.operationPollTimer = null;
    try {
      const response = await api("/api/operation");
      const operation = response.data?.operation || null;
      if (state.status) state.status.operation = operation;
      renderOperation(operation);
      updateActionAvailability(Boolean(state.status?.service?.active), operation);
    } catch { /* The regular status refresh remains the fallback. */ }
    if (state.status?.operation?.state === "running") scheduleOperationPoll();
  }, 800);
}

function renderOperation(operation) {
  const banner = $("#operationBanner");
  if (operation && state.status) state.status.operation = operation;
  if (operation?.name === "settings") {
    state.localSettingsOperation = null;
    state.settingsSubmitting = false;
  }
  if (operation?.state !== "running" && operation?.finished_at) {
    const age = Date.now() - new Date(operation.finished_at).getTime();
    if (Number.isFinite(age) && age > 120000) {
      state.operationDismissed = operation.id;
      if (operation.name === "settings") state.settingsOperationDismissed = operation.id;
    }
  }
  const dismissed = !operation || state.operationDismissed === operation.id;
  renderSettingsOperation(dismissed ? null : operation);
  if (dismissed || operation.name === "settings") {
    banner.classList.add("is-hidden");
  } else {
    banner.classList.remove("is-hidden", "is-error", "is-success");
    banner.classList.toggle("is-error", operation.state === "error");
    banner.classList.toggle("is-success", operation.state === "success");
    $("#operationSpinner").classList.toggle("is-hidden", operation.state !== "running");
    $("#operationTitle").textContent = operation.state === "running" ? operation.label : `${operation.label}${operation.state === "success" ? "完成" : "失败"}`;
    $("#operationMessage").textContent = operation.message || "请稍候…";
    $("#dismissOperation").classList.toggle("is-hidden", operation.state === "running");
  }
  if (dismissed) {
    stopOperationPolling();
    syncSettingsControls(operation);
    return;
  }

  if (operation.state === "running") scheduleOperationPoll();
  else stopOperationPolling();

  const notificationKey = `${operation.id}:${operation.state}`;
  if (operation.state !== "running" && state.operationNotified !== notificationKey) {
    state.operationNotified = notificationKey;
    toast(operation.state === "success" ? `${operation.label}完成` : `${operation.label}失败`, operation.message || "", operation.state === "error");
    refreshStatus(false);
    loadBackups();
    loadChecks(false);
    if (operation.name === "settings" && operation.state === "success") {
      state.changes.clear();
      updateSettingsSavebar();
      loadSettings();
    }
  }
  if (operation.name !== "settings" && operation.state === "success" && state.operationAutoDismiss !== operation.id) {
    state.operationAutoDismiss = operation.id;
    window.setTimeout(() => {
      if (state.status?.operation?.id === operation.id) {
        state.operationDismissed = operation.id;
        banner.classList.add("is-hidden");
      }
    }, 8000);
  }
}

function syncSettingsControls(operation = state.status?.operation) {
  const anyBusy = state.settingsSubmitting || operation?.state === "running";
  const settingsBusy = state.settingsSubmitting || (operation?.name === "settings" && operation.state === "running");
  const apply = $("#applySettings");
  const discard = $("#discardSettings");
  if (apply) {
    apply.disabled = anyBusy || state.changes.size === 0;
    apply.textContent = settingsBusy ? "已提交，正在应用…" : (anyBusy ? "请等待当前操作" : "应用并安全重启");
  }
  if (discard) discard.disabled = settingsBusy;
  $("#settingsSearch")?.toggleAttribute("disabled", settingsBusy);
  $("#settingsList")?.classList.toggle("is-busy", settingsBusy);
  $$(`#categoryTabs button`).forEach((button) => { button.disabled = settingsBusy; });
  $$(`#settingsList input, #settingsList select`).forEach((input) => {
    input.disabled = settingsBusy || Boolean(input.closest(".setting-row")?.classList.contains("is-locked"));
  });
}

function updateActionAvailability(serviceActive, operation) {
  const busy = operation?.state === "running";
  $$(`[data-action]`).forEach((button) => {
    const action = button.dataset.action;
    let disabled = busy;
    if (["save", "stop", "restart", "announce"].includes(action) && !serviceActive) disabled = true;
    if (action === "start" && serviceActive) disabled = true;
    button.disabled = disabled;
  });
  $("#announceInput").disabled = busy || !serviceActive;
  $("#announceForm button").disabled = busy || !serviceActive;
  syncSettingsControls(operation);
}

function renderHomeOverall() {
  const badge = $("#overallBadge");
  if (!badge || !state.status) return;
  badge.classList.remove("good", "bad");
  const service = state.status.service || {};
  if (!service.active && service.manual_stop) {
    badge.textContent = "按要求停止";
    return;
  }
  if (!state.status.healthy || state.checks?.overall === "error") {
    badge.textContent = "存在异常";
    badge.classList.add("bad");
    return;
  }
  if (state.checks?.overall === "warning") {
    badge.textContent = `${state.checks.counts?.warning || 0} 项提醒`;
    return;
  }
  badge.textContent = "运行正常";
  badge.classList.add("good");
}

function renderStatus(data) {
  state.status = data;
  const service = data.service || {};
  const host = data.host || {};
  const metrics = data.metrics || {};
  const active = Boolean(service.active);
  const healthy = Boolean(data.healthy);
  const players = normalizePlayers(data.players);
  const fps = Number(metrics.serverfpsaverage ?? metrics.serverfps);

  const livePill = $("#livePill");
  livePill.classList.toggle("is-online", healthy);
  livePill.classList.toggle("is-offline", !active);
  $("#liveText").textContent = healthy ? "运行正常" : (active ? "状态降级" : (service.manual_stop ? "已手动停止" : "服务已停止"));
  $("#serverOrb").className = `server-orb ${healthy ? "is-online" : "is-offline"}`;
  $("#heroStatus").textContent = healthy ? "游戏服务运行正常" : (active ? "服务正在运行，但有项目未响应" : (service.manual_stop ? "游戏服务已按要求停止" : "游戏服务意外停止"));
  $("#heroName").textContent = service.server_name || "幻兽帕鲁服务器";
  $("#heroDetail").textContent = healthy ? "游戏端口、管理接口与世界状态均已响应" : (service.manual_stop ? "健康检查会尊重这个状态，不会自动启动游戏" : (service.api_error || "服务当前不可用"));
  $("#gameVersion").textContent = service.game_version || "—";
  $("#steamBuild").textContent = service.installed_build || "—";
  $("#serviceUptime").textContent = formatDuration(service.uptime_seconds);
  $("#sideVersion").textContent = `v${data.panel_version || "1"}`;
  $("#homeGameState").textContent = healthy ? "运行正常" : (service.manual_stop ? "已手动停止" : (active ? "需要检查" : "意外停止"));
  $("#homeGameDetail").textContent = healthy ? "端口与管理接口正常" : (service.api_error || "等待服务响应");
  $("#homePlayerState").textContent = `${players.length} 人`;
  $("#homeFpsState").textContent = Number.isFinite(fps) ? `${fps.toFixed(1)} FPS` : "暂无数据";
  $("#homeResourceState").textContent = `${formatPercent(host.cpu_percent, 0)} / ${formatPercent(host.memory_percent, 0)}`;
  $("#homeResourceDetail").textContent = "CPU / 内存";
  $("#homeCheckedAt").textContent = `最近刷新：${formatDate(data.checked_at)}`;
  renderHomeOverall();

  const cpu = host.cpu_percent;
  $("#cpuValue").textContent = formatPercent(cpu, cpu < 10 ? 1 : 0);
  $("#cpuTemp").textContent = host.temperature_c ? `${Number(host.temperature_c).toFixed(0)}°C` : "温度 —";
  $("#cpuDetail").textContent = `游戏占整机 ${formatPercent(data.game_cpu_host_percent, 1)} · ${host.logical_cpus || 12} 线程`;
  setMeter($("#cpuMeter"), cpu);

  $("#memoryValue").textContent = formatPercent(host.memory_percent, 0);
  $("#gameMemory").textContent = `游戏 ${formatBytes(service.memory_bytes)}`;
  $("#memoryDetail").textContent = `${formatBytes(host.memory_used_bytes)} / ${formatBytes(host.memory_total_bytes)}`;
  setMeter($("#memoryMeter"), host.memory_percent);

  $("#diskValue").textContent = formatPercent(host.disk_percent, 0);
  $("#backupSize").textContent = `备份 ${formatBytes(data.backups?.total_bytes)}`;
  const ssdTemperature = Number(host.ssd_temperature_c);
  const ssdText = Number.isFinite(ssdTemperature) ? `SSD ${ssdTemperature.toFixed(0)}°C · ` : "";
  $("#diskDetail").textContent = `${ssdText}可用 ${formatBytes(host.disk_free_bytes)} / ${formatBytes(host.disk_total_bytes)}`;
  setMeter($("#diskMeter"), host.disk_percent);

  $("#fpsValue").textContent = Number.isFinite(fps) ? fps.toFixed(1) : "—";
  setMeter($("#fpsMeter"), Number.isFinite(fps) ? fps / 60 * 100 : 0, false);
  const frameTime = Number(metrics.serverframetime);
  $("#frameDetail").textContent = Number.isFinite(frameTime) ? `帧时间 ${frameTime.toFixed(2)} ms · 客户端渲染帧率无限制` : "服务器模拟帧率，不是本机画面帧率";

  const rx = host.network_rx_bytes_per_second || 0;
  const tx = host.network_tx_bytes_per_second || 0;
  $("#networkRate").textContent = `网络 ↓ ${formatBytes(rx)}/s  ↑ ${formatBytes(tx)}/s`;
  $("#hostUptime").textContent = `主机运行 ${formatDuration(host.uptime_seconds)}`;

  renderPlayers(players, metrics.maxplayernum || metrics.maxPlayerNum || 2);
  renderAutomation(data.timers || []);
  renderUpdate(data.last_update);
  renderOperation(data.operation);
  updateActionAvailability(active, data.operation);

  if (Number.isFinite(Number(cpu)) && Number.isFinite(Number(host.memory_percent))) {
    state.chart.push({ cpu: Number(cpu), memory: Number(host.memory_percent) });
    if (state.chart.length > 60) state.chart.shift();
    drawChart();
  }
}

async function refreshStatus(showError = false) {
  if (!state.authenticated || state.statusLoading) return;
  state.statusLoading = true;
  $("#refreshButton").classList.add("is-spinning");
  try {
    const response = await api("/api/status");
    renderStatus(response.data);
  } catch (exception) {
    if (showError) toast("无法读取服务器状态", exception.message, true);
    $("#livePill").classList.remove("is-online");
    $("#livePill").classList.add("is-offline");
    $("#liveText").textContent = "连接失败";
  } finally {
    state.statusLoading = false;
    $("#refreshButton").classList.remove("is-spinning");
  }
}

function renderActiveCheckGroup() {
  const container = $("#checksGroups");
  container.replaceChildren();
  const groups = state.checks?.groups || [];
  const group = groups.find((item) => item.id === state.activeCheckGroup) || groups[0];
  if (!group) return;

  const section = document.createElement("section");
  section.className = "check-group";
  section.setAttribute("role", "tabpanel");
  section.setAttribute("aria-label", group.title);
  const heading = document.createElement("div");
  heading.className = "check-group-head";
  const title = document.createElement("h3");
  title.textContent = group.title;
  const summary = document.createElement("span");
  summary.textContent = group.summary || `${group.checks?.length || 0} 项`;
  heading.append(title, summary);
  section.append(heading);
  (group.checks || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = `check-item ${item.status || "info"}`;
    const dot = document.createElement("span");
    dot.className = "check-dot";
    const copy = document.createElement("div");
    copy.className = "check-item-copy";
    const label = document.createElement("strong");
    label.textContent = item.label;
    copy.append(label);
    if (item.detail) {
      const detail = document.createElement("p");
      detail.textContent = item.detail;
      copy.append(detail);
    }
    const value = document.createElement("span");
    value.className = "check-value";
    value.textContent = item.value || "—";
    row.append(dot, copy, value);
    section.append(row);
  });
  container.append(section);
}

function renderCheckGroupTabs() {
  const container = $("#checkGroupTabs");
  container.replaceChildren();
  (state.checks?.groups || []).forEach((group) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `check-group-tab${group.id === state.activeCheckGroup ? " is-active" : ""}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(group.id === state.activeCheckGroup));
    const head = document.createElement("span");
    head.className = "check-group-tab-head";
    const dot = document.createElement("span");
    dot.className = "check-dot";
    button.classList.add(group.status || "info");
    const label = document.createElement("strong");
    label.textContent = group.title;
    const count = document.createElement("small");
    count.textContent = `${group.checks?.length || 0} 项检查`;
    head.append(dot, label);
    button.append(head, count);
    button.addEventListener("click", () => {
      state.activeCheckGroup = group.id;
      renderCheckGroupTabs();
      renderActiveCheckGroup();
    });
    container.append(button);
  });
}

function renderChecks(data) {
  state.checks = data;
  const counts = data.counts || {};
  const healthy = data.overall === "healthy";
  const warning = data.overall === "warning";
  const orb = $("#checkOrb");
  orb.className = `check-orb${warning ? " warning" : (healthy ? "" : " error")}`;
  orb.querySelector("span").textContent = healthy ? "✓" : (warning ? "!" : "×");
  $("#checkOverall").textContent = healthy ? "所有关键项目正常" : (warning ? "关键服务正常，有项目需要留意" : "发现需要处理的异常");
  $("#checkSummaryText").textContent = `共检查 ${counts.total || 0} 项：${counts.pass || 0} 项正常，${counts.warning || 0} 项提醒，${counts.fail || 0} 项异常。`;
  $("#checkPassCount").textContent = counts.pass ?? 0;
  $("#checkWarningCount").textContent = counts.warning ?? 0;
  $("#checkFailCount").textContent = counts.fail ?? 0;
  $("#checkCheckedAt").textContent = formatDate(data.checked_at, true);
  $("#checksState").classList.add("is-hidden");

  const groups = data.groups || [];
  if (!groups.some((group) => group.id === state.activeCheckGroup)) {
    const attention = groups.find((group) => group.status === "fail" || group.status === "warning");
    state.activeCheckGroup = (attention || groups[0])?.id || null;
  }
  renderCheckGroupTabs();
  renderActiveCheckGroup();
  renderHomeOverall();
}

async function loadChecks(showError = false) {
  if (!state.authenticated || state.checksLoading) return;
  state.checksLoading = true;
  const button = $("#refreshChecks");
  button.disabled = true;
  button.textContent = "正在检查…";
  if (!state.checks) $("#checksState").classList.remove("is-hidden");
  try {
    const response = await api("/api/checks");
    renderChecks(response.data);
    if (showError) toast("检查已完成", "结果已更新");
  } catch (exception) {
    if (showError) toast("完整检查失败", exception.message, true);
    if (!state.checks) {
      $("#checksState").classList.remove("is-hidden");
      $("#checksState").textContent = `检查失败：${exception.message}`;
    }
  } finally {
    state.checksLoading = false;
    button.disabled = false;
    button.textContent = "重新检查";
  }
}

function drawChart() {
  const canvas = $("#loadChart");
  if (!canvas || !canvas.isConnected) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  const top = 8, bottom = 18, left = 4, right = 4;
  const graphHeight = height - top - bottom;
  const styles = getComputedStyle(document.documentElement);
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = styles.getPropertyValue("--chart-grid").trim() || "rgba(93, 109, 132, .16)";
  ctx.lineWidth = 1;
  [0, 25, 50, 75, 100].forEach((value) => {
    const y = top + graphHeight * (1 - value / 100);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
  });
  if (state.chart.length < 2) return;
  const drawLine = (key, color) => {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    state.chart.forEach((point, index) => {
      const x = left + index / 59 * (width - left - right);
      const y = top + graphHeight * (1 - clamp(point[key]) / 100);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  drawLine("memory", styles.getPropertyValue("--blue").trim() || "#66a5ff");
  drawLine("cpu", styles.getPropertyValue("--accent").trim() || "#3578e5");
}

function eventCategory(kind) {
  const value = String(kind || "system");
  if (value.startsWith("update")) return "update";
  if (value.startsWith("restart") || value === "stop") return "restart";
  return value;
}

function renderHistoryTimeline(timeline, isClient) {
  const section = $("#historyTimeline");
  section.classList.toggle("is-hidden", isClient);
  if (isClient) return;
  const rail = $("#historyEventRail");
  const list = $("#historyEventList");
  rail.replaceChildren();
  list.replaceChildren();
  const events = Array.isArray(timeline?.events) ? timeline.events : [];
  const counts = timeline?.counts || {};
  const countLabels = [
    ["fps_drop", "掉帧"], ["players", "人数变化"], ["save", "保存"],
    ["backup", "备份"], ["update", "更新"], ["restart", "重启"],
  ].filter(([kind]) => Number(counts[kind]) > 0).map(([kind, label]) => `${label} ${counts[kind]}`);
  $("#eventTimelineSummary").textContent = countLabels.length
    ? `${countLabels.join(" · ")}；标记与上方曲线使用同一时间坐标${timeline?.truncated ? "（仅显示最近 800 项）" : ""}。`
    : "这个范围内没有掉帧、人数变化或运维事件。";
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "event-list-empty";
    empty.textContent = "暂无可对齐事件";
    list.append(empty);
    return;
  }
  const start = Date.parse(timeline.start_at || events[0].timestamp);
  const end = Date.parse(timeline.end_at || events[events.length - 1].timestamp);
  const span = Math.max(1, end - start);
  const lanes = { fps_drop: 0, players: 1, save: 2, backup: 2, update: 3, restart: 3 };
  events.forEach((event) => {
    const timestamp = Date.parse(event.timestamp);
    if (!Number.isFinite(timestamp)) return;
    const category = eventCategory(event.kind);
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = `event-marker event-kind-${String(event.kind || "system").replace(/[^a-z0-9_-]/gi, "")}`;
    marker.style.left = `${clamp((timestamp - start) / span * 100, 0, 100)}%`;
    marker.style.setProperty("--event-lane", String(lanes[category] ?? 3));
    const description = `${formatDate(event.timestamp, true)} · ${event.title}${event.detail ? ` · ${event.detail}` : ""}`;
    marker.title = description;
    marker.setAttribute("aria-label", description);
    marker.setAttribute("role", "listitem");
    rail.append(marker);
  });
  events.slice(-12).reverse().forEach((event) => {
    const item = document.createElement("article");
    item.className = `event-item event-kind-${String(event.kind || "system").replace(/[^a-z0-9_-]/gi, "")}`;
    const copy = document.createElement("div");
    const timeNode = document.createElement("time");
    timeNode.dateTime = event.timestamp;
    timeNode.textContent = formatDate(event.timestamp, true);
    const title = document.createElement("strong");
    title.textContent = event.title || "系统事件";
    copy.append(timeNode, title);
    if (event.detail) {
      const detail = document.createElement("small");
      detail.textContent = event.detail;
      detail.title = event.detail;
      copy.append(detail);
    }
    item.append(copy);
    list.append(item);
  });
}

function renderPerformanceHistory(data) {
  state.history = data;
  const isClient = data.source === "client";
  const recorder = data.recorder || {};
  const recorderBar = $("#historyRecorderState").parentElement;
  recorderBar.classList.remove("is-on", "is-error");
  if (recorder.error || (!isClient && !recorder.running)) {
    recorderBar.classList.add("is-error");
    $("#historyRecorderState").lastChild.textContent = recorder.error ? "后台记录异常" : "后台记录未运行";
  } else if (isClient && !recorder.running) {
    const lastSample = recorder.last_sample_at ? ` · 最后采样 ${formatDate(recorder.last_sample_at, true)}` : "";
    $("#historyRecorderState").lastChild.textContent = recorder.row_count
      ? `游玩电脑当前未上报${lastSample}`
      : "等待游玩电脑首次采样";
  } else {
    recorderBar.classList.add("is-on");
    const lastSample = recorder.last_sample_at ? ` · 最近采样 ${formatDate(recorder.last_sample_at, true)}` : " · 等待首次采样";
    const stateText = isClient && recorder.game_running ? "游玩电脑正在游戏" : "后台记录正常";
    $("#historyRecorderState").lastChild.textContent = `${stateText}${lastSample}`;
  }
  $("#historyRetention").textContent = isClient
    ? `游戏中 ${recorder.sample_interval_seconds || 10} 秒 / 空闲 ${recorder.idle_sample_interval_seconds || 60} 秒 · 保留 ${recorder.retention_days || 365} 天 · ${recorder.row_count || 0} 条 · 总库 ${formatBytes(recorder.database_size_bytes)} / ${formatBytes(recorder.database_hard_limit_bytes)}`
    : `每 ${recorder.sample_interval_seconds || 30} 秒 · 保留 ${recorder.retention_days || 365} 天 · ${recorder.row_count || 0} 条 · 总库 ${formatBytes(recorder.database_size_bytes)} / ${formatBytes(recorder.database_hard_limit_bytes)}`;

  const summary = data.summary || {};
  if (!isClient && summary.peak_ssd_temperature_c !== null && summary.peak_ssd_temperature_c !== undefined
      && Number.isFinite(Number(summary.peak_ssd_temperature_c))) {
    $("#historyRetention").textContent += ` · SSD 峰值 ${Number(summary.peak_ssd_temperature_c).toFixed(0)}°C`;
  }
  if (isClient) {
    const temperature = formatNumber(summary.peak_gpu_temperature_c, 0);
    const clientValues = [
      ["记录覆盖", formatDuration(summary.sampled_seconds)],
      ["实际游玩", formatDuration(summary.play_seconds)],
      ["本机 CPU P95", formatPercent(summary.p95_cpu_percent, 0)],
      ["显卡负载 P95", formatPercent(summary.p95_gpu_percent, 0)],
      ["游戏内存峰值", formatBytes(summary.peak_game_memory_bytes)],
      ["显卡峰值温度", temperature === "—" ? "—" : `${temperature}°C`],
    ];
    const valueElements = [$("#historyPlayTime"), $("#historyMaxPlayers"), $("#historyAverageFps"), $("#historyMinimumFps"), $("#historyP95Cpu"), $("#historyP95Memory")];
    clientValues.forEach(([label, value], index) => {
      $(`#historyLabel${index + 1}`).textContent = label;
      valueElements[index].textContent = value;
    });
  } else {
    ["有效记录", "最高在线", "平均 FPS", "最低 FPS", "游戏 CPU P95", "内存 P95"].forEach((label, index) => {
      $(`#historyLabel${index + 1}`).textContent = label;
    });
    $("#historyPlayTime").textContent = formatDuration(summary.sampled_seconds);
    $("#historyMaxPlayers").textContent = `${summary.max_players_observed || 0} 人`;
    $("#historyAverageFps").textContent = formatNumber(summary.average_fps, 1);
    $("#historyMinimumFps").textContent = formatNumber(summary.minimum_fps, 1);
    const p95GameCpu = formatNumber(summary.p95_game_cpu_one_core_percent, 0);
    $("#historyP95Cpu").textContent = p95GameCpu === "—" ? "—" : `${p95GameCpu}% 单核`;
    $("#historyP95Memory").textContent = formatPercent(summary.p95_memory_percent, 0);
  }

  $("#historyLoading").classList.add("is-hidden");
  const hasPoints = Array.isArray(data.points) && data.points.length > 0;
  $("#historyEmpty").classList.toggle("is-hidden", hasPoints);
  $("#historyContent").classList.toggle("is-hidden", !hasPoints);
  if (!hasPoints) {
    const copy = $("#historyEmpty div");
    const playing = data.range === "playing";
    copy.querySelector("strong").textContent = playing
      ? (isClient ? "尚无本机游玩时样本" : "尚无玩家在线时的样本")
      : "这个时间范围还没有记录";
    copy.querySelector("p").textContent = isClient
      ? "本机采集器已经待命；下次启动游戏后会自动切换为每 10 秒采样。"
      : (playing
        ? "记录器已经待命；你下次进入世界后，这里会自动积累服务器 FPS、CPU、内存和人数数据。"
        : "后台记录刚刚开始，后续采样会自动出现在这里。");
    return;
  }

  $("#serverCapacity").classList.toggle("is-hidden", isClient);
  $("#clientSessions").classList.toggle("is-hidden", !isClient);
  $("#historyLegend4").classList.toggle("is-hidden", isClient);
  renderHistoryTimeline(data.timeline || {}, isClient);
  if (isClient) {
    $("#historyChartCaption").textContent = `${data.range_label || "性能历史"} · 本机 CPU、显卡与内存百分比`;
    $("#historyLegend1").textContent = "本机 CPU";
    $("#historyLegend2").textContent = "显卡";
    $("#historyLegend3").textContent = "内存";
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    $("#clientSessionCount").textContent = sessions.length ? `最近 ${sessions.length} 次` : "尚未形成样本";
    const sessionRows = $("#clientSessionRows");
    sessionRows.replaceChildren();
    if (!sessions.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.textContent = "游戏进程启动后会自动形成一次会话记录。";
      row.append(cell);
      sessionRows.append(row);
    } else {
      sessions.forEach((session) => {
        const temperature = formatNumber(session.peak_gpu_temperature_c, 0);
        const latency = formatNumber(session.average_upload_latency_ms, 1);
        const values = [
          [formatDate(session.started_at, true), "开始时间"],
          [formatDuration(session.sampled_seconds), "有效时长"],
          [formatPercent(session.average_cpu_percent, 0), "平均 CPU"],
          [formatPercent(session.average_gpu_percent, 0), "平均 GPU"],
          [temperature === "—" ? "—" : `${temperature}°C`, "GPU 峰值温度"],
          [formatBytes(session.peak_game_memory_bytes), "游戏内存峰值"],
          [latency === "—" ? "—" : `${latency} ms`, "上传延迟"],
        ];
        const row = document.createElement("tr");
        values.forEach(([value, label]) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          cell.dataset.label = label;
          row.append(cell);
        });
        sessionRows.append(row);
      });
    }
  } else {
    $("#historyChartCaption").textContent = `${data.range_label || "性能历史"} · FPS 按 60 满帧归一，CPU / 内存为百分比`;
    $("#historyLegend1").textContent = "FPS";
    $("#historyLegend2").textContent = "CPU";
    $("#historyLegend3").textContent = "内存";
    $("#historyLegend4").textContent = "SSD 温度";
  }
  if (isClient) {
    window.requestAnimationFrame(drawHistoryChart);
    return;
  }
  const capacity = data.capacity || {};
  $("#capacityMessage").textContent = capacity.message || "正在积累用于承载能力判断的数据。";
  $("#capacityStable").textContent = capacity.observed_stable_players
    ? `最高实测稳定 ${capacity.observed_stable_players} 人`
    : "尚未形成稳定人数档";
  const rows = $("#capacityRows");
  rows.replaceChildren();
  const buckets = Array.isArray(capacity.buckets) ? capacity.buckets : [];
  if (!buckets.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = "暂无玩家在线样本；进入世界后会按实际在线人数自动分档。";
    row.append(cell);
    rows.append(row);
  } else {
    buckets.forEach((bucket) => {
      const enough = Number(bucket.sample_count) >= Number(capacity.minimum_samples_per_player_count || 20);
      const stable = enough && Number(bucket.p10_fps) >= 55 && (!Number.isFinite(Number(bucket.p95_memory_percent)) || Number(bucket.p95_memory_percent) < 85);
      const values = [
        [`${bucket.player_count} 人`, "在线人数"],
        [formatDuration(bucket.sampled_seconds), "有效时长"],
        [`${formatNumber(bucket.average_fps, 1)} / ${formatNumber(bucket.minimum_fps, 1)}`, "平均 / 最低 FPS"],
        [formatPercent(bucket.p95_game_cpu_one_core_percent, 0), "游戏 CPU P95"],
        [formatPercent(bucket.p95_memory_percent, 0), "内存 P95"],
        [enough ? (stable ? "目前稳定" : "存在压力") : "继续采样", "判断"],
      ];
      const row = document.createElement("tr");
      values.forEach(([value, label], index) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        cell.dataset.label = label;
        if (index === 5) cell.className = stable ? "is-stable" : "is-warning";
        row.append(cell);
      });
      rows.append(row);
    });
  }
  window.requestAnimationFrame(drawHistoryChart);
}

async function loadPerformanceHistory(showError = false) {
  if (!state.authenticated || state.historyLoading) return;
  state.historyLoading = true;
  const requestedRange = state.historyRange;
  const requestedSource = state.historySource;
  if (!state.history) $("#historyLoading").classList.remove("is-hidden");
  try {
    const response = await api(`/api/performance-history?source=${encodeURIComponent(requestedSource)}&range=${encodeURIComponent(requestedRange)}`);
    if (requestedRange !== state.historyRange || requestedSource !== state.historySource) return;
    state.historyLoadedAt = Date.now();
    renderPerformanceHistory(response.data);
  } catch (exception) {
    state.historyLoadedAt = Date.now();
    if (showError) toast("性能记录读取失败", exception.message, true);
    if (!state.history) {
      $("#historyLoading").classList.add("is-hidden");
      $("#historyEmpty").classList.remove("is-hidden");
      $("#historyEmpty strong").textContent = "性能记录暂时不可用";
      $("#historyEmpty p").textContent = exception.message;
    }
  } finally {
    state.historyLoading = false;
    if (requestedRange !== state.historyRange || requestedSource !== state.historySource) loadPerformanceHistory(false);
  }
}

function drawHistoryChart() {
  const canvas = $("#historyChart");
  const points = state.history?.points || [];
  if (!canvas || !canvas.isConnected || !points.length || $("#historyContent").classList.contains("is-hidden")) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  const top = 10, bottom = 26, left = 34, right = 10;
  const graphWidth = width - left - right;
  const graphHeight = height - top - bottom;
  const styles = getComputedStyle(document.documentElement);
  const grid = styles.getPropertyValue("--chart-grid").trim() || "rgba(93, 109, 132, .16)";
  const subtle = styles.getPropertyValue("--subtle").trim() || "#8190a6";
  ctx.clearRect(0, 0, width, height);
  ctx.font = '9px "Segoe UI", sans-serif';
  ctx.textBaseline = "middle";
  ctx.textAlign = "right";
  [0, 25, 50, 75, 100].forEach((value) => {
    const y = top + graphHeight * (1 - value / 100);
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
    ctx.fillStyle = subtle;
    ctx.fillText(String(value), left - 7, y);
  });
  if (state.history?.source !== "client") {
    const targetY = top + graphHeight * (1 - 55 / 60);
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = styles.getPropertyValue("--amber").trim() || "#c77b16";
    ctx.beginPath(); ctx.moveTo(left, targetY); ctx.lineTo(width - right, targetY); ctx.stroke();
    ctx.restore();
  }

  const isFiniteValue = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
  const pointTimes = points.map((point) => Date.parse(point.timestamp));
  const timelineStart = Date.parse(state.history?.timeline?.start_at || "");
  const timelineEnd = Date.parse(state.history?.timeline?.end_at || "");
  const rangeStart = Number.isFinite(timelineStart) ? timelineStart : pointTimes[0];
  const rangeEnd = Number.isFinite(timelineEnd) ? timelineEnd : pointTimes[pointTimes.length - 1];
  const timeSpan = Math.max(1, rangeEnd - rangeStart);
  const xForTime = (value) => left + clamp((value - rangeStart) / timeSpan, 0, 1) * graphWidth;
  const xAt = (index) => points.length === 1 ? left + graphWidth / 2 : xForTime(pointTimes[index]);

  if (state.history?.source !== "client") {
    const eventColors = {
      fps_drop: styles.getPropertyValue("--danger").trim() || "#d34b57",
      players: styles.getPropertyValue("--accent").trim() || "#3578e5",
      save: styles.getPropertyValue("--success").trim() || "#168866",
      backup: styles.getPropertyValue("--blue").trim() || "#66a5ff",
      update: styles.getPropertyValue("--amber").trim() || "#c77b16",
      restart: styles.getPropertyValue("--muted").trim() || "#5d6d84",
    };
    const eventLanes = { fps_drop: 0, players: 1, save: 2, backup: 2, update: 3, restart: 3 };
    (state.history?.timeline?.events || []).slice(-300).forEach((event) => {
      const timestamp = Date.parse(event.timestamp);
      if (!Number.isFinite(timestamp)) return;
      const category = eventCategory(event.kind);
      const color = eventColors[category] || eventColors.restart;
      const x = xForTime(timestamp);
      if (category === "fps_drop" && event.end_timestamp) {
        const endTimestamp = Date.parse(event.end_timestamp);
        if (Number.isFinite(endTimestamp)) {
          const endX = xForTime(endTimestamp);
          ctx.save();
          ctx.globalAlpha = .08;
          ctx.fillStyle = color;
          ctx.fillRect(Math.min(x, endX), top, Math.max(2, Math.abs(endX - x)), graphHeight);
          ctx.restore();
        }
      }
      ctx.save();
      ctx.globalAlpha = category === "fps_drop" ? .34 : .19;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + graphHeight); ctx.stroke();
      ctx.globalAlpha = .92;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, top + 4 + (eventLanes[category] ?? 3) * 5, 2.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    });
  }
  const drawLine = (key, color, transform = (value) => value) => {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    let started = false;
    points.forEach((point, index) => {
      if (!isFiniteValue(point[key])) { started = false; return; }
      const x = xAt(index);
      const y = top + graphHeight * (1 - clamp(transform(Number(point[key]))) / 100);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
    if (points.length === 1 && isFiniteValue(points[0][key])) {
      const y = top + graphHeight * (1 - clamp(transform(Number(points[0][key]))) / 100);
      ctx.fillStyle = color; ctx.beginPath(); ctx.arc(xAt(0), y, 3, 0, Math.PI * 2); ctx.fill();
    }
  };
  if (state.history?.source === "client") {
    drawLine("client_cpu_percent", styles.getPropertyValue("--success").trim() || "#168866");
    drawLine("gpu_util_percent", styles.getPropertyValue("--accent").trim() || "#3578e5");
    drawLine("memory_percent", styles.getPropertyValue("--blue").trim() || "#66a5ff");
  } else {
    drawLine("server_fps", styles.getPropertyValue("--success").trim() || "#168866", (value) => value / 60 * 100);
    drawLine("host_cpu_percent", styles.getPropertyValue("--accent").trim() || "#3578e5");
    drawLine("memory_percent", styles.getPropertyValue("--blue").trim() || "#66a5ff");
    drawLine("ssd_temperature_c", styles.getPropertyValue("--amber").trim() || "#c77b16");
  }

  ctx.fillStyle = subtle;
  ctx.textBaseline = "bottom";
  ctx.textAlign = "left";
  ctx.fillText(formatDate(points[0].timestamp, true), left, height);
  ctx.textAlign = "right";
  ctx.fillText(formatDate(points[points.length - 1].timestamp, true), width - right, height);
}

async function downloadPerformanceHistory() {
  const button = $("#downloadPerformance");
  button.disabled = true;
  button.textContent = "正在导出…";
  try {
    const response = await fetch(`/api/download/performance-history?source=${encodeURIComponent(state.historySource)}&range=${encodeURIComponent(state.historyRange)}`, { credentials: "same-origin" });
    if (response.status === 401) { showLogin(); throw new Error("登录已失效"); }
    if (!response.ok) {
      let message = `性能记录导出失败（HTTP ${response.status}）`;
      try { message = (await response.json()).error || message; } catch { /* Keep HTTP message. */ }
      throw new Error(message);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const utfName = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const filename = utfName ? decodeURIComponent(utfName[1]) : `palworld-performance-${Date.now()}.csv`;
    const objectUrl = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    toast("性能记录已下载", "CSV 可直接用于表格分析或交给 AI 评估承载能力。");
  } catch (exception) {
    toast("性能记录下载失败", exception.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "下载 CSV";
  }
}

async function confirmDialog({ title, message, confirmText = "确认", expected = null, danger = true }) {
  const modal = $("#confirmModal");
  $("#modalTitle").textContent = title;
  $("#modalMessage").textContent = message;
  $("#modalConfirm").textContent = confirmText;
  $("#modalConfirm").className = `button ${danger ? "button-danger" : "button-primary"}`;
  const wrap = $("#confirmInputWrap");
  const input = $("#confirmInput");
  wrap.classList.toggle("is-hidden", expected === null);
  input.value = "";
  input.dataset.expected = expected || "";
  $("#confirmInputLabel").textContent = expected ? `请输入“${expected}”以确认` : "";
  $("#modalConfirm").disabled = expected !== null;
  modal.classList.remove("is-hidden");
  if (expected !== null) window.setTimeout(() => input.focus(), 80);
  return new Promise((resolve) => { state.modalResolve = resolve; });
}

function closeModal(value) {
  $("#confirmModal").classList.add("is-hidden");
  if (state.modalResolve) state.modalResolve(value);
  state.modalResolve = null;
}

async function performAction(action, extras = {}, skipConfirm = false) {
  const confirmations = {
    "update-apply": ["安装服务器更新？", "仅在检测到新构建时停服。操作前会保存并创建更新前备份。", "开始更新", false],
    stop: ["停止游戏服务器？", "会先保存并创建校验备份。停止后健康检查会尊重该状态，不会自动启动。", "安全停止", true],
    restart: ["安全重启服务器？", "会先保存世界并创建校验备份，确认无人在线后重启。", "安全重启", false],
    maintenance: ["执行清理维护？", "将清理过期日志和超出轮换策略的备份，不会删除保留范围内的存档。", "开始维护", false],
  };
  if (!skipConfirm && confirmations[action]) {
    const [title, message, text, danger] = confirmations[action];
    if (!await confirmDialog({ title, message, confirmText: text, danger })) return;
  }
  try {
    const response = await api("/api/action", {
      method: "POST", body: JSON.stringify({ action, ...extras }),
    });
    state.operationDismissed = null;
    renderOperation(response.operation);
    window.setTimeout(() => refreshStatus(false), 500);
  } catch (exception) {
    toast("操作未开始", exception.message, true);
  }
}

async function loadSettings() {
  try {
    const response = await api("/api/settings");
    state.settings = response.data.settings || [];
    state.settingsCategories = response.data.categories || [];
    state.changes.clear();
    renderCategoryTabs();
    renderSettings();
    updateSettingsSavebar();
  } catch (exception) {
    $("#settingsState").classList.remove("is-hidden");
    $("#settingsState").textContent = `读取设置失败：${exception.message}`;
  }
}

function renderCategoryTabs() {
  const container = $("#categoryTabs");
  container.replaceChildren();
  ["全部", ...state.settingsCategories].forEach((category) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = category;
    button.disabled = state.settingsSubmitting || (state.status?.operation?.name === "settings" && state.status.operation.state === "running");
    button.classList.toggle("is-active", state.category === category);
    button.addEventListener("click", () => {
      state.category = category;
      renderCategoryTabs();
      renderSettings();
    });
    container.append(button);
  });
}

function currentSettingValue(item) {
  return state.changes.has(item.key) ? state.changes.get(item.key) : item.value;
}

function valuesEqual(left, right) {
  if (typeof left === "number" && typeof right === "number") return Math.abs(left - right) < 1e-9;
  return left === right;
}

function updateSettingChange(item, value, row) {
  if (valuesEqual(value, item.value)) state.changes.delete(item.key);
  else state.changes.set(item.key, value);
  row.classList.toggle("is-changed", state.changes.has(item.key));
  updateSettingsSavebar();
}

function makeSettingControl(item, row) {
  const container = document.createElement("div");
  container.className = "setting-control";
  const value = currentSettingValue(item);
  let input;
  if (item.type === "boolean") {
    container.classList.add("switch-control");
    const stateLabel = document.createElement("span");
    stateLabel.className = `switch-state${value ? " is-on" : ""}`;
    stateLabel.textContent = value ? "已开启" : "已关闭";
    const label = document.createElement("label");
    label.className = "switch";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.disabled = !item.editable || state.settingsSubmitting || (state.status?.operation?.name === "settings" && state.status.operation.state === "running");
    input.setAttribute("aria-label", item.label);
    const visual = document.createElement("span");
    label.append(input, visual);
    container.append(stateLabel, label);
    input.addEventListener("change", () => {
      stateLabel.textContent = input.checked ? "已开启" : "已关闭";
      stateLabel.classList.toggle("is-on", input.checked);
      updateSettingChange(item, input.checked, row);
    });
    return container;
  }
  if (item.type === "select") {
    input = document.createElement("select");
    (item.choices || []).forEach((choice) => {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = item.choice_labels?.[choice] || choice;
      input.append(option);
    });
  } else {
    input = document.createElement("input");
    input.type = ["integer", "number"].includes(item.type) ? "number" : "text";
    if (item.min !== null && item.min !== undefined) input.min = item.min;
    if (item.max !== null && item.max !== undefined) input.max = item.max;
    if (input.type === "number") input.step = item.step || (item.type === "integer" ? 1 : .1);
  }
  input.className = "setting-input";
  input.setAttribute("aria-label", item.label);
  input.value = value ?? "";
  input.disabled = !item.editable || state.settingsSubmitting || (state.status?.operation?.name === "settings" && state.status.operation.state === "running");
  input.addEventListener(item.type === "text" ? "input" : "change", () => {
    let next = input.value;
    if (item.type === "integer") next = Number.parseInt(input.value, 10);
    if (item.type === "number") next = Number.parseFloat(input.value);
    if ((item.type === "integer" || item.type === "number") && Number.isNaN(next)) {
      input.setCustomValidity("请输入有效数字");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    updateSettingChange(item, next, row);
  });
  container.append(input);
  return container;
}

function renderSettings() {
  const list = $("#settingsList");
  list.replaceChildren();
  const query = state.settingsSearch.trim().toLocaleLowerCase("zh-CN");
  const filtered = state.settings.filter((item) => {
    const category = state.category === "全部" || item.category === state.category;
    const search = !query || `${item.label} ${item.key} ${item.help || ""}`.toLocaleLowerCase("zh-CN").includes(query);
    return category && search;
  });
  $("#settingsState").classList.toggle("is-hidden", filtered.length > 0);
  if (!filtered.length) {
    $("#settingsState").textContent = state.settings.length ? "没有匹配的设置项" : "正在读取世界设置…";
    return;
  }
  const groups = new Map();
  filtered.forEach((item) => {
    if (!groups.has(item.category)) groups.set(item.category, []);
    groups.get(item.category).push(item);
  });
  groups.forEach((items, category) => {
    const section = document.createElement("section");
    section.className = "settings-group";
    const heading = document.createElement("h3");
    heading.textContent = `${category} · ${items.length}`;
    const grid = document.createElement("div");
    grid.className = "settings-grid";
    items.forEach((item) => {
      const row = document.createElement("article");
      row.className = `setting-row${state.changes.has(item.key) ? " is-changed" : ""}${item.editable ? "" : " is-locked"}`;
      row.dataset.key = item.key;
      const info = document.createElement("div");
      const title = document.createElement("div");
      title.className = "setting-title";
      const strong = document.createElement("strong");
      strong.textContent = item.label;
      title.append(strong);
      if (item.impact) {
        const badge = document.createElement("span");
        badge.className = "impact-badge";
        badge.textContent = item.impact === "high" ? "性能敏感" : "影响负载";
        title.append(badge);
      }
      const key = document.createElement("span");
      key.className = "setting-key";
      key.textContent = `${item.key}${item.editable ? "" : " · 由服务器基础设施锁定"}`;
      info.append(title, key);
      if (item.help) {
        const help = document.createElement("p");
        help.className = "setting-help";
        help.textContent = item.help;
        info.append(help);
      }
      row.append(info, makeSettingControl(item, row));
      grid.append(row);
    });
    section.append(heading, grid);
    list.append(section);
  });
}

function updateSettingsSavebar() {
  const count = state.changes.size;
  const progressVisible = !$("#settingsOperation").classList.contains("is-hidden");
  $("#settingsSavebar").classList.toggle("is-hidden", count === 0 || progressVisible || state.settingsSubmitting);
  $("#changeCount").textContent = `${count} 项待应用`;
  $("#changeNavCount").textContent = count;
  $("#changeNavCount").classList.toggle("is-hidden", count === 0);
  syncSettingsControls();
}

async function applySettings() {
  if (!state.changes.size) return;
  if (state.settingsSubmitting || state.status?.operation?.state === "running") {
    toast("暂时不能应用设置", "服务器正在执行另一项操作，请等待当前进度完成。", true);
    return;
  }
  const count = state.changes.size;
  const approved = await confirmDialog({
    title: `应用 ${count} 项世界设置？`,
    message: "服务器会确认无人在线，保存世界并创建校验备份，然后只重启一次。任何一步失败都会恢复原配置。",
    confirmText: "应用并安全重启",
    danger: false,
  });
  if (!approved) return;
  if (state.settingsSubmitting || state.status?.operation?.state === "running") {
    toast("暂时不能应用设置", "确认期间服务器开始了另一项操作，请稍后再试。", true);
    return;
  }
  const changes = Object.fromEntries(state.changes.entries());
  const localOperation = {
    id: `local-${Date.now()}`,
    name: "settings",
    label: `应用 ${count} 项世界设置`,
    state: "running",
    phase: "submitting",
    progress: 2,
    message: "正在把改动提交给服务器，提交后会自动保存、备份、重启并检查服务。",
    change_count: count,
    started_at: new Date().toISOString(),
    finished_at: null,
    local: true,
  };
  state.settingsSubmitting = true;
  state.settingsOperationDismissed = null;
  state.localSettingsOperation = localOperation;
  renderSettingsOperation(localOperation);
  try {
    const response = await api("/api/settings", { method: "POST", body: JSON.stringify({ changes }) });
    state.settingsSubmitting = false;
    state.localSettingsOperation = null;
    state.operationDismissed = null;
    state.settingsOperationDismissed = null;
    if (state.status) state.status.operation = response.operation;
    renderOperation(response.operation);
    updateActionAvailability(Boolean(state.status?.service?.active), response.operation);
    window.setTimeout(() => refreshStatus(false), 500);
  } catch (exception) {
    state.settingsSubmitting = false;
    state.localSettingsOperation = {
      ...localOperation,
      state: "error",
      phase: "error",
      progress: 2,
      message: `请求未能确认：${exception.message}`,
      finished_at: new Date().toISOString(),
    };
    renderSettingsOperation(state.localSettingsOperation);
    toast("设置未应用", exception.message, true);
    refreshStatus(false);
  }
}

async function downloadBackup(backup) {
  const url = `/api/download/backup?id=${encodeURIComponent(backup.id)}`;
  try {
    const response = await fetch(url, { method: "HEAD", credentials: "same-origin" });
    if (response.status === 401) {
      showLogin();
      throw new Error("登录已失效");
    }
    if (!response.ok) {
      let message = `下载准备失败（HTTP ${response.status}）`;
      try { message = (await response.json()).error || message; } catch { /* Keep HTTP message. */ }
      throw new Error(message);
    }
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = backup.name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    toast("备份下载已开始", backup.name);
  } catch (exception) {
    toast("备份下载失败", exception.message, true);
  }
}

async function downloadDiagnostics() {
  const button = $("#downloadDiagnostics");
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    const response = await fetch("/api/download/diagnostics", { credentials: "same-origin" });
    if (response.status === 401) {
      showLogin();
      throw new Error("登录已失效");
    }
    if (!response.ok) {
      let message = `诊断包生成失败（HTTP ${response.status}）`;
      try { message = (await response.json()).error || message; } catch { /* Keep HTTP message. */ }
      throw new Error(message);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const utfName = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const filename = utfName ? decodeURIComponent(utfName[1]) : `palworld-diagnostics-${Date.now()}.zip`;
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    toast("诊断包已下载", "可直接提供给 AI 排查，密码内容已隐藏。 ");
  } catch (exception) {
    toast("诊断包下载失败", exception.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "下载诊断包";
  }
}

async function loadBackups() {
  try {
    const response = await api("/api/backups");
    state.backups = response.data.items || [];
    renderBackups(response.data);
  } catch (exception) {
    $("#backupList").textContent = `读取备份失败：${exception.message}`;
  }
}

function renderBackups(data) {
  $("#backupCount").textContent = data.count ?? state.backups.length;
  $("#backupTotal").textContent = formatBytes(data.total_bytes);
  const list = $("#backupList");
  list.replaceChildren();
  if (!state.backups.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = "<span>□</span><p>暂无受管备份</p>";
    list.append(empty);
    return;
  }
  state.backups.forEach((backup, index) => {
    const row = document.createElement("article");
    row.className = "backup-row";
    const name = document.createElement("div");
    name.className = "backup-name";
    const strong = document.createElement("strong");
    strong.textContent = backup.name;
    const sub = document.createElement("span");
    sub.textContent = index === 0 ? "最新备份 · 创建时已完整校验" : "创建时已完整校验";
    name.append(strong, sub);
    const kind = document.createElement("div");
    kind.className = "backup-cell";
    const kindBadge = document.createElement("span");
    kindBadge.className = "backup-kind";
    kindBadge.textContent = kindNames[backup.kind] || backup.kind;
    const date = document.createElement("strong");
    date.textContent = formatDate(backup.created_at);
    kind.append(kindBadge, date);
    const size = document.createElement("div");
    size.className = "backup-cell";
    const sizeValue = document.createElement("strong");
    sizeValue.textContent = formatBytes(backup.size_bytes);
    const sizeLabel = document.createElement("span");
    sizeLabel.textContent = "压缩后大小";
    size.append(sizeValue, sizeLabel);
    const actions = document.createElement("div");
    actions.className = "backup-actions";
    const download = document.createElement("button");
    download.className = "mini-button";
    download.textContent = "下载";
    download.addEventListener("click", () => downloadBackup(backup));
    const verify = document.createElement("button");
    verify.className = "mini-button";
    verify.textContent = "完整校验";
    verify.addEventListener("click", () => performAction("verify-backup", { backup: backup.id }, true));
    const restore = document.createElement("button");
    restore.className = "mini-button danger";
    restore.textContent = "恢复";
    restore.addEventListener("click", async () => {
      const approved = await confirmDialog({
        title: "恢复这份备份？",
        message: "恢复前会再创建一份当前世界备份；恢复后必须通过启动健康检查，否则自动回滚。此操作会覆盖活动世界。",
        confirmText: "恢复备份",
        expected: backup.name,
        danger: true,
      });
      if (approved) performAction("restore-backup", { backup: backup.id, confirmation: `RESTORE:${backup.id}` }, true);
    });
    actions.append(download, verify, restore);
    row.append(name, kind, size, actions);
    list.append(row);
  });
}

async function loadLogs() {
  $("#logOutput").textContent = "正在读取日志…";
  try {
    const response = await api("/api/logs?lines=140");
    state.logs = response.data;
    renderLogs();
  } catch (exception) {
    $("#logOutput").textContent = `读取日志失败：${exception.message}`;
  }
}

function renderLogs() {
  $("#logOutput").textContent = state.logs[state.activeLog] || "暂无日志。";
  $$(`[data-log]`).forEach((button) => button.classList.toggle("is-active", button.dataset.log === state.activeLog));
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", handleLogin);
  $("#logoutButton").addEventListener("click", logout);
  $("#refreshButton").addEventListener("click", () => {
    refreshStatus(true);
    state.historyLoadedAt = 0;
    loadPerformanceHistory(true);
    if (state.section === "checks") loadChecks(false);
  });
  $("#refreshChecks").addEventListener("click", () => loadChecks(true));
  $("#downloadDiagnostics").addEventListener("click", downloadDiagnostics);
  $("#themeMenuButton").addEventListener("click", () => {
    const menu = $("#themeMenu");
    const opening = menu.classList.contains("is-hidden");
    menu.classList.toggle("is-hidden", !opening);
    $("#themeMenuButton").classList.toggle("is-active", opening);
    $("#themeMenuButton").setAttribute("aria-expanded", String(opening));
  });
  $$(`[data-theme-value]`).forEach((button) => button.addEventListener("click", () => {
    applyTheme(button.dataset.themeValue);
    $("#themeMenu").classList.add("is-hidden");
    $("#themeMenuButton").classList.remove("is-active");
    $("#themeMenuButton").setAttribute("aria-expanded", "false");
    drawChart();
    drawHistoryChart();
  }));
  $("#darkModeButton").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? lastLightTheme : "dark");
    drawChart();
    drawHistoryChart();
  });
  $$(`[data-history-source]`).forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.historySource === state.historySource) return;
    state.historySource = button.dataset.historySource;
    state.history = null;
    state.historyLoadedAt = 0;
    $$(`[data-history-source]`).forEach((item) => item.classList.toggle("is-active", item === button));
    $("#historyContent").classList.add("is-hidden");
    $("#historyEmpty").classList.add("is-hidden");
    $("#historyLoading").classList.remove("is-hidden");
    loadPerformanceHistory(true);
  }));
  $$(`[data-history-range]`).forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.historyRange === state.historyRange) return;
    state.historyRange = button.dataset.historyRange;
    state.history = null;
    state.historyLoadedAt = 0;
    $$(`[data-history-range]`).forEach((item) => item.classList.toggle("is-active", item === button));
    $("#historyContent").classList.add("is-hidden");
    $("#historyEmpty").classList.add("is-hidden");
    $("#historyLoading").classList.remove("is-hidden");
    loadPerformanceHistory(true);
  }));
  $("#downloadPerformance").addEventListener("click", downloadPerformanceHistory);
  $$(`[data-section]`).forEach((button) => button.addEventListener("click", () => navigate(button.dataset.section)));
  $$(`[data-action]`).forEach((button) => button.addEventListener("click", () => performAction(button.dataset.action)));
  $("#announceForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = $("#announceInput").value.trim();
    if (!message) return;
    performAction("announce", { message }, true);
    $("#announceInput").value = "";
  });
  $("#settingsSearch").addEventListener("input", (event) => { state.settingsSearch = event.target.value; renderSettings(); });
  $("#discardSettings").addEventListener("click", () => { state.changes.clear(); renderSettings(); updateSettingsSavebar(); });
  $("#applySettings").addEventListener("click", applySettings);
  $("#refreshLogs").addEventListener("click", loadLogs);
  $$(`[data-log]`).forEach((button) => button.addEventListener("click", () => { state.activeLog = button.dataset.log; renderLogs(); }));
  $("#dismissOperation").addEventListener("click", () => {
    state.operationDismissed = state.status?.operation?.id || null;
    $("#operationBanner").classList.add("is-hidden");
  });
  $("#dismissSettingsOperation").addEventListener("click", () => {
    const operation = state.status?.operation?.name === "settings" ? state.status.operation : state.localSettingsOperation;
    state.settingsOperationDismissed = operation?.id || null;
    state.localSettingsOperation = null;
    $("#settingsOperation").classList.add("is-hidden");
    updateSettingsSavebar();
  });
  $("#modalCancel").addEventListener("click", () => closeModal(false));
  $("#modalConfirm").addEventListener("click", () => closeModal(true));
  $("#confirmInput").addEventListener("input", (event) => {
    $("#modalConfirm").disabled = event.target.value !== event.target.dataset.expected;
  });
  $("#confirmModal").addEventListener("click", (event) => { if (event.target.id === "confirmModal") closeModal(false); });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".theme-picker")) {
      $("#themeMenu").classList.add("is-hidden");
      $("#themeMenuButton").classList.remove("is-active");
      $("#themeMenuButton").setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#confirmModal").classList.contains("is-hidden")) closeModal(false);
    $("#themeMenu").classList.add("is-hidden");
    $("#themeMenuButton").classList.remove("is-active");
    $("#themeMenuButton").setAttribute("aria-expanded", "false");
  });
  window.addEventListener("resize", () => { drawChart(); drawHistoryChart(); });
}

async function initialize() {
  bindEvents();
  try {
    const response = await api("/api/session");
    if (response.authenticated) await showApp();
    else showLogin();
  } catch {
    showLogin();
  }
}

document.addEventListener("DOMContentLoaded", initialize);
