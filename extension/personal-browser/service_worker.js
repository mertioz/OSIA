const PROTOCOL_VERSION = "1.0";
const HOST_NAME = "space.osia.browser";
const MAX_MESSAGE_BYTES = 256 * 1024;
const CAPABILITIES = [
  "get_browser_state", "inspect_browser_page", "inspect_form_fields",
  "navigate_browser", "list_browser_tabs", "select_browser_tab",
  "close_browser_tab", "reload_browser_page", "browser_back",
  "browser_forward", "activate_browser_element",
  "fill_form_field", "wait_for_browser_condition", "revoke",
  "prepare_browser_action_authorization"
];

let nativePort = null;
let connectionId = "";
let installInstanceId = "";
let browserSessionId = "";
let browserFamily = "chrome";
let authorizations = {};
const cancelled = new Set();

const opaque = (prefix) => `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
const connectionOpaque = () => `connection:${crypto.randomUUID().replaceAll("-", "")}`;
const requestSafe = (value) => value && typeof value === "object" && !Array.isArray(value);

async function initialize() {
  const local = await chrome.storage.local.get("install_instance_id");
  installInstanceId = local.install_instance_id || opaque("install");
  if (!local.install_instance_id) await chrome.storage.local.set({ install_instance_id: installInstanceId });
  const session = await chrome.storage.session.get(["browser_session_id", "authorizations"]);
  browserSessionId = session.browser_session_id || opaque("personal");
  if (!session.browser_session_id) await chrome.storage.session.set({ browser_session_id: browserSessionId });
  authorizations = requestSafe(session.authorizations) ? session.authorizations : {};
  const ua = navigator.userAgent || "";
  browserFamily = /Edg\//.test(ua) ? "edge" : "chrome";
  try {
    if (navigator.brave && await navigator.brave.isBrave()) browserFamily = "brave";
  } catch (_) {}
}

const initialized = initialize();

function messageSize(value) {
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

function post(message) {
  if (!nativePort || messageSize(message) > MAX_MESSAGE_BYTES) return false;
  nativePort.postMessage(message);
  return true;
}

async function ensurePort() {
  await initialized;
  if (nativePort) return nativePort;
  connectionId = connectionOpaque();
  nativePort = chrome.runtime.connectNative(HOST_NAME);
  nativePort.onMessage.addListener(handleNativeMessage);
  nativePort.onDisconnect.addListener(async () => {
    nativePort = null;
    connectionId = "";
    await revokeAll("disconnected");
  });
  post({
    protocol_version: PROTOCOL_VERSION,
    message_type: "hello",
    protocol_min: PROTOCOL_VERSION,
    protocol_max: PROTOCOL_VERSION,
    connection_id: connectionId,
    install_instance_id: installInstanceId,
    browser_session_id: browserSessionId,
    browser_family: browserFamily,
    caller_origin: `chrome-extension://${chrome.runtime.id}/`,
    capabilities: CAPABILITIES
  });
  for (const record of Object.values(authorizations)) postTarget(record);
  return nativePort;
}

function safePage(url) {
  try {
    const parsed = new URL(url || "");
    if (!/^https?:$/.test(parsed.protocol)) return { allowed: false, sensitive: true };
    const host = parsed.hostname.toLowerCase();
    const path = parsed.pathname.toLowerCase();
    const extensionStore = host === "chromewebstore.google.com" ||
      (host === "chrome.google.com" && path.startsWith("/webstore")) ||
      (host === "microsoftedge.microsoft.com" && path.startsWith("/addons"));
    const sensitive = host === "accounts.google.com" || extensionStore ||
      /(^|[._-])(oauth|login|signin|auth|payment|checkout)([._-]|$)/.test(host) ||
      /\/(oauth|oauth2|login|signin|auth|authorize|sso|consent|payment|checkout)(\/|$)/.test(path);
    return {
      allowed: true,
      sensitive,
      origin: parsed.origin,
      safe_url: `${parsed.origin}${parsed.pathname}`.slice(0, 1024)
    };
  } catch (_) {
    return { allowed: false, sensitive: true };
  }
}

function stateRef(record) {
  return {
    schema_version: 1,
    browser_session_id: browserSessionId,
    context_id: record.context_id,
    context_epoch: 1,
    page_id: record.page_id,
    page_generation: 1,
    navigation_id: record.navigation_id,
    document_epoch: record.document_epoch,
    created_at: record.created_at
  };
}

function publicTarget(record, authorized = true) {
  return {
    target_handle: record.target_handle,
    install_instance_id: installInstanceId,
    browser_session_id: browserSessionId,
    browser_family: browserFamily,
    window_id: record.window_id,
    tab_id: record.tab_id,
    safe_origin: record.origin,
    safe_url: record.safe_url,
    safe_title: String(record.safe_title || "").slice(0, 160),
    authorization_epoch: record.authorization_epoch,
    document_nonce: record.document_nonce,
    state_ref: authorized ? stateRef(record) : null,
    authorized,
    state: authorized ? (record.sensitive ? "sensitive_page_blocked" : "ready") : "authorization_revoked"
  };
}

function postTarget(record, authorized = true) {
  post({
    protocol_version: PROTOCOL_VERSION,
    message_type: "target_update",
    connection_id: connectionId,
    target: publicTarget(record, authorized)
  });
}

async function persist() {
  await chrome.storage.session.set({ authorizations });
}

async function injectAndChallenge(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "osia.challenge" });
    if (response && response.ok) return response.result;
  } catch (_) {}
  await chrome.scripting.executeScript({
    target: { tabId, allFrames: false },
    files: ["content_agent.js"],
    world: "ISOLATED"
  });
  const response = await chrome.tabs.sendMessage(tabId, { type: "osia.challenge" });
  if (!response || !response.ok) throw new Error("stale_target");
  return response.result;
}

async function authorizeTab(tab) {
  await ensurePort();
  if (!tab || !Number.isInteger(tab.id) || !Number.isInteger(tab.windowId)) throw new Error("stale_target");
  const page = safePage(tab.url);
  if (!page.allowed) throw new Error("sensitive_page_blocked");
  const challenge = page.sensitive ? {
    document_nonce: `document:${crypto.randomUUID().replaceAll("-", "")}`,
    title: tab.title || ""
  } : await injectAndChallenge(tab.id);
  for (const existing of Object.values(authorizations)) {
    if (existing.tab_id === tab.id && existing.window_id === tab.windowId) {
      await revokeRecord(existing);
    }
  }
  const now = Date.now() / 1000;
  const record = {
    target_handle: `target:${crypto.randomUUID().replaceAll("-", "")}`,
    tab_id: tab.id,
    window_id: tab.windowId,
    origin: page.origin,
    safe_url: page.safe_url,
    safe_title: String(tab.title || challenge.title || "").slice(0, 160),
    authorization_epoch: 1,
    document_nonce: challenge.document_nonce,
    context_id: opaque("context"),
    page_id: opaque("page"),
    navigation_id: opaque("nav"),
    document_epoch: 0,
    created_at: now,
    sensitive: page.sensitive
  };
  authorizations[record.target_handle] = record;
  await persist();
  postTarget(record);
  await chrome.action.setBadgeBackgroundColor({ tabId: tab.id, color: page.sensitive ? "#B45309" : "#5B21B6" });
  await chrome.action.setBadgeText({ tabId: tab.id, text: page.sensitive ? "!" : "ON" });
}

chrome.action.onClicked.addListener((tab) => {
  authorizeTab(tab).catch(async () => {
    if (tab && Number.isInteger(tab.id)) {
      await chrome.action.setBadgeBackgroundColor({ tabId: tab.id, color: "#B91C1C" });
      await chrome.action.setBadgeText({ tabId: tab.id, text: "ERR" });
    }
  });
});

async function revokeRecord(record, reason = "authorization_revoked") {
  delete authorizations[record.target_handle];
  await persist();
  postTarget(record, false);
  try { await chrome.action.setBadgeText({ tabId: record.tab_id, text: "" }); } catch (_) {}
  return reason;
}

async function revokeAll(reason) {
  const records = Object.values(authorizations);
  authorizations = {};
  await persist();
  for (const record of records) {
    postTarget(record, false);
    try { await chrome.action.setBadgeText({ tabId: record.tab_id, text: "" }); } catch (_) {}
  }
  return reason;
}

async function revalidate(record) {
  let tab;
  try { tab = await chrome.tabs.get(record.tab_id); } catch (_) { await revokeRecord(record, "stale_target"); throw new Error("stale_target"); }
  if (!tab || tab.windowId !== record.window_id) {
    await revokeRecord(record, "stale_target");
    throw new Error("stale_target");
  }
  const page = safePage(tab.url);
  if (!page.allowed || page.origin !== record.origin) {
    await revokeRecord(record, "authorization_revoked");
    throw new Error("authorization_revoked");
  }
  if (page.sensitive) {
    record.safe_url = page.safe_url;
    record.safe_title = String(tab.title || "").slice(0, 160);
    record.sensitive = true;
    await persist();
    postTarget(record);
    return record;
  }
  let challenge;
  try { challenge = await injectAndChallenge(record.tab_id); }
  catch (_) { await revokeRecord(record, "authorization_revoked"); throw new Error("authorization_revoked"); }
  if (challenge.document_nonce !== record.document_nonce) {
    record.document_nonce = challenge.document_nonce;
    record.navigation_id = opaque("nav");
    record.document_epoch += 1;
  }
  record.safe_url = page.safe_url;
  record.safe_title = String(tab.title || challenge.title || "").slice(0, 160);
  record.sensitive = page.sensitive;
  await persist();
  postTarget(record);
  return record;
}

function validateState(record, provided) {
  if (!provided) return;
  const current = stateRef(record);
  for (const key of Object.keys(current)) {
    if (provided[key] !== current[key]) throw new Error("stale_browser_reference");
  }
}

function receipt(before, after, dispatched, success, evidence = []) {
  const now = Date.now() / 1000;
  return {
    schema_version: 1,
    operation_id: opaque("op"),
    tool_call_id: opaque("call"),
    before_state: before,
    after_state: after,
    dispatch_state: success ? "effect_confirmed" : dispatched ? "effect_unknown" : "not_dispatched",
    outcome: success ? "confirmed_success" : dispatched ? "outcome_unknown" : "confirmed_failure_before_effect",
    evidence: evidence.slice(0, 16),
    started_at: now,
    completed_at: Date.now() / 1000
  };
}

function successResult(before, after, data, evidence) {
  return {
    ok: true,
    receipt: receipt(before, after, true, true, evidence),
    text: "Browser operation completed with verified evidence.",
    data
  };
}

function failureResult(code, before = null, dispatched = false) {
  return {
    ok: false,
    receipt: receipt(before, before, dispatched, false),
    text: `ERROR: personal browser operation failed (${code})`,
    error: { code }
  };
}

async function sha256Hex(value) {
  const ordered = {};
  for (const key of Object.keys(value).sort()) ordered[key] = value[key];
  const bytes = new TextEncoder().encode(JSON.stringify(ordered));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(
    new Uint8Array(digest),
    (item) => item.toString(16).padStart(2, "0")
  ).join("");
}

async function contentMessage(tabId, message) {
  const response = await chrome.tabs.sendMessage(tabId, message);
  if (!response || !response.ok) throw new Error(response && response.error && response.error.code || "browser_operation_failed");
  return response.result;
}

async function executeRequest(message) {
  const record = authorizations[message.target_handle];
  if (!record) return failureResult("stale_target");
  if (message.operation === "revoke") {
    const before = stateRef(record);
    post({ protocol_version: PROTOCOL_VERSION, message_type: "dispatch_started", request_id: message.request_id, connection_id: connectionId, target_handle: record.target_handle });
    await revokeRecord(record);
    return successResult(before, null, { revoked: true }, [{ authorization_revoked: true }]);
  }
  await revalidate(record);
  const before = stateRef(record);
  validateState(record, message.parameters && message.parameters.state_ref);
  if (record.sensitive) return failureResult("manual_intervention_required", before, false);
  const operation = message.operation;
  const parameters = message.parameters || {};
  if (!CAPABILITIES.includes(operation)) return failureResult("capability_unavailable", before, false);
  if (cancelled.has(message.request_id)) return failureResult("cancelled", before, false);

  if (operation === "get_browser_state") {
    return successResult(before, before, { state: before, safe_url: record.safe_url, lifecycle_events: [] }, [{ state_observed: true }]);
  }
  if (operation === "inspect_browser_page") {
    const observed = await contentMessage(record.tab_id, { type: "osia.inspect_page", max_items: parameters.max_items });
    return successResult(before, before, { state: before, ...observed }, [{ page_inspected: true }]);
  }
  if (operation === "inspect_form_fields") {
    const observed = await contentMessage(record.tab_id, { type: "osia.inspect_forms", max_fields: parameters.max_fields });
    return successResult(before, before, { state: before, ...observed }, [{ forms_inspected: true }]);
  }
  if (operation === "list_browser_tabs") {
    const tabs = Object.values(authorizations).map((item) => ({
      state: stateRef(item), safe_url: item.safe_url, safe_title: item.safe_title,
      active: item.target_handle === record.target_handle
    }));
    return successResult(before, before, { state: before, tabs }, [{ tabs_observed: tabs.length }]);
  }
  if (operation === "prepare_browser_action_authorization") {
    const metadata = await contentMessage(record.tab_id, {
      type: "osia.prepare_action", element_ref: parameters.element_ref
    });
    return {
      trusted_final_action: metadata.trusted_final_action === true,
      destination_origin: String(metadata.destination_origin || record.origin).slice(0, 512),
      trusted_target_binding: await sha256Hex({
        ...metadata,
        document_nonce: record.document_nonce,
        element_ref: parameters.element_ref,
        target_handle: record.target_handle
      })
    };
  }

  post({ protocol_version: PROTOCOL_VERSION, message_type: "dispatch_started", request_id: message.request_id, connection_id: connectionId, target_handle: record.target_handle });
  if (cancelled.has(message.request_id)) return failureResult("cancelled", before, false);

  if (operation === "navigate_browser") {
    const destination = safePage(parameters.url);
    if (!destination.allowed || destination.sensitive) return failureResult("sensitive_page_blocked", before, false);
    await chrome.tabs.update(record.tab_id, { url: parameters.url });
    if (destination.origin !== record.origin) {
      await revokeRecord(record);
      return successResult(before, null, { authorization_revoked: true, safe_url: destination.safe_url }, [{ navigation_committed: true }, { authorization_revoked: true }]);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
    await revalidate(record);
    const after = stateRef(record);
    return successResult(before, after, { state: after, safe_url: record.safe_url }, [{ navigation_committed: true }]);
  }
  if (operation === "select_browser_tab") {
    await chrome.tabs.update(record.tab_id, { active: true });
    await chrome.windows.update(record.window_id, { focused: true });
    return successResult(before, before, { state: before }, [{ tab_activated: true }]);
  }
  if (operation === "close_browser_tab") {
    await chrome.tabs.remove(record.tab_id);
    await revokeRecord(record);
    return successResult(before, null, { closed: true }, [{ tab_closed: true }]);
  }
  if (["reload_browser_page", "browser_back", "browser_forward"].includes(operation)) {
    if (operation === "reload_browser_page") await chrome.tabs.reload(record.tab_id);
    else if (operation === "browser_back") await chrome.tabs.goBack(record.tab_id);
    else await chrome.tabs.goForward(record.tab_id);
    await new Promise((resolve) => setTimeout(resolve, 250));
    await revalidate(record);
    const after = stateRef(record);
    return successResult(before, after, { state: after, safe_url: record.safe_url }, [{ navigation_committed: true }]);
  }
  if (operation === "activate_browser_element") {
    const result = await contentMessage(record.tab_id, { type: "osia.activate", element_ref: parameters.element_ref });
    await revalidate(record);
    const after = stateRef(record);
    return successResult(before, after, { state: after, ...result }, [{ element_activated: true }]);
  }
  if (operation === "fill_form_field") {
    const result = await contentMessage(record.tab_id, { type: "osia.fill", field_ref: parameters.field_ref, value: parameters.value });
    return successResult(before, before, { state: before, ...result }, [{ field_filled: true }]);
  }
  if (operation === "wait_for_browser_condition") {
    const result = await contentMessage(record.tab_id, { type: "osia.wait", wait_spec: parameters.wait_spec || {} });
    await revalidate(record);
    const after = stateRef(record);
    return successResult(before, after, { state: after, ...result }, [{ condition_matched: true }]);
  }
  if (operation === "return_to_browser_opener" || operation === "list_browser_dialogs" || operation === "handle_browser_dialog") {
    return failureResult("capability_unavailable", before, false);
  }
  return failureResult("capability_unavailable", before, false);
}

async function handleNativeMessage(message) {
  if (!requestSafe(message) || message.protocol_version !== PROTOCOL_VERSION || messageSize(message) > MAX_MESSAGE_BYTES) return;
  if (message.message_type === "cancel" && message.connection_id === connectionId) {
    cancelled.add(message.request_id);
    return;
  }
  if (message.message_type !== "request" || message.connection_id !== connectionId) return;
  let result;
  try {
    if (Date.now() > message.deadline_ms) result = failureResult("browser_timeout");
    else result = await executeRequest(message);
  } catch (error) {
    const code = /^[a-z][a-z0-9_]{1,63}$/.test(String(error && error.message || "")) ? error.message : "browser_operation_failed";
    result = failureResult(code);
  } finally {
    cancelled.delete(message.request_id);
  }
  post({
    protocol_version: PROTOCOL_VERSION,
    message_type: "response",
    request_id: message.request_id,
    connection_id: connectionId,
    status: "ok",
    error: null,
    result
  });
}

chrome.runtime.onInstalled.addListener(() => initialized.then(() => revokeAll("extension_updated")));
chrome.runtime.onStartup.addListener(() => initialized.then(() => revokeAll("browser_restarted")));
