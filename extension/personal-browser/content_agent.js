(() => {
  "use strict";

  if (globalThis.__osiaPersonalBrowserAgent) return;

  const MAX_ITEMS = 80;
  const MAX_TEXT = 8000;
  const documentNonce = `document:${crypto.randomUUID().replaceAll("-", "")}`;
  const elements = new Map();

  const opaque = (prefix) => `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
  const bounded = (value, maximum) => String(value || "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, maximum);

  function safeLocation() {
    return {
      origin: location.origin,
      url: `${location.origin}${location.pathname}`.slice(0, 1024),
      title: bounded(document.title, 160),
    };
  }

  function labelFor(element) {
    const aria = element.getAttribute("aria-label");
    if (aria) return bounded(aria, 180);
    if (element.id) {
      try {
        const label = element.ownerDocument.querySelector(`label[for="${CSS.escape(element.id)}"]`);
        if (label) return bounded(label.innerText || label.textContent, 180);
      } catch (_) {}
    }
    return bounded(element.innerText || element.textContent || element.getAttribute("name") || "", 180);
  }

  function isSensitiveField(element) {
    const type = String(element.getAttribute("type") || "").toLowerCase();
    const autocomplete = String(element.getAttribute("autocomplete") || "").toLowerCase();
    const identity = `${element.getAttribute("name") || ""} ${element.id || ""} ${labelFor(element)}`.toLowerCase();
    return type === "password" || autocomplete.includes("one-time-code") || /\b(otp|2fa|mfa|passcode|verification code)\b/.test(identity);
  }

  function roots() {
    const output = [document];
    for (const frame of document.querySelectorAll("iframe")) {
      try {
        if (frame.contentDocument && frame.contentWindow.location.origin === location.origin) {
          output.push(frame.contentDocument);
        }
      } catch (_) {}
      if (output.length >= 16) break;
    }
    return output;
  }

  function remember(element, kind) {
    const ref = opaque(kind);
    elements.set(ref, element);
    return ref;
  }

  function inspectPage(maxItems) {
    elements.clear();
    const limit = Math.max(1, Math.min(Number(maxItems) || 40, MAX_ITEMS));
    const items = [];
    for (const root of roots()) {
      const candidates = root.querySelectorAll("a[href],button,input,textarea,select,[role='button'],[role='link']");
      for (const element of candidates) {
        if (items.length >= limit) break;
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const tag = element.tagName.toLowerCase();
        const kind = tag === "a" ? "link" : ["input", "textarea", "select"].includes(tag) ? "control" : "button";
        const item = {
          kind,
          element_ref: remember(element, kind),
          label: labelFor(element),
          disabled: Boolean(element.disabled || element.getAttribute("aria-disabled") === "true"),
        };
        if (tag === "a") {
          try {
            const parsed = new URL(element.href, location.href);
            item.safe_url = `${parsed.origin}${parsed.pathname}`.slice(0, 1024);
          } catch (_) {}
        }
        if (kind === "control") {
          item.input_type = bounded(element.getAttribute("type") || tag, 32);
          item.sensitive = isSensitiveField(element);
        }
        items.push(item);
      }
      if (items.length >= limit) break;
    }
    const visibleText = bounded(document.body ? document.body.innerText : "", MAX_TEXT);
    return { ...safeLocation(), items, visible_text: visibleText, truncated: visibleText.length >= MAX_TEXT };
  }

  function inspectForms(maxFields) {
    elements.clear();
    const limit = Math.max(1, Math.min(Number(maxFields) || 50, MAX_ITEMS));
    const fields = [];
    for (const root of roots()) {
      for (const element of root.querySelectorAll("input,textarea,select")) {
        if (fields.length >= limit) break;
        if (element.type === "hidden" || isSensitiveField(element)) continue;
        fields.push({
          field_ref: remember(element, "field"),
          label: labelFor(element),
          input_type: bounded(element.getAttribute("type") || element.tagName.toLowerCase(), 32),
          required: Boolean(element.required),
          disabled: Boolean(element.disabled),
          read_only: Boolean(element.readOnly),
        });
      }
      if (fields.length >= limit) break;
    }
    return { ...safeLocation(), fields, truncated: fields.length >= limit };
  }

  function activate(elementRef) {
    const element = elements.get(String(elementRef || ""));
    if (!element || !element.isConnected) throw new Error("stale_element_reference");
    if (isSensitiveField(element)) throw new Error("sensitive_page_blocked");
    element.scrollIntoView({ block: "center", inline: "center" });
    element.click();
    return { activated: true };
  }

  function prepareAction(elementRef) {
    const element = elements.get(String(elementRef || ""));
    if (!element || !element.isConnected) throw new Error("stale_element_reference");
    if (isSensitiveField(element)) throw new Error("manual_intervention_required");
    const tag = element.tagName.toLowerCase();
    const type = String(element.getAttribute("type") || "").toLowerCase();
    const role = String(element.getAttribute("role") || "").toLowerCase();
    const form = element.form || element.closest("form");
    let destinationOrigin = location.origin;
    if (element.href) {
      try { destinationOrigin = new URL(element.href, location.href).origin; } catch (_) {}
    }
    return {
      tag, type, role,
      destination_origin: destinationOrigin,
      in_form: Boolean(form),
      form_method: String(form && form.method || "").toLowerCase(),
      trusted_final_action: type === "submit" || (tag === "button" && Boolean(form)) || role === "submit" || String(element.href || "").startsWith("mailto:") || String(element.href || "").startsWith("tel:")
    };
  }

  function fillField(fieldRef, value) {
    const element = elements.get(String(fieldRef || ""));
    if (!element || !element.isConnected) throw new Error("stale_element_reference");
    if (isSensitiveField(element) || element.disabled || element.readOnly) throw new Error("sensitive_page_blocked");
    const text = String(value ?? "").slice(0, 16000);
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, text);
    else element.value = text;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return { filled: true, length: text.length };
  }

  async function waitCondition(spec) {
    const timeout = Math.max(50, Math.min(120000, Number(spec && spec.timeout_seconds || 10) * 1000));
    const deadline = Date.now() + timeout;
    const condition = String(spec && spec.condition || "");
    const expected = spec && spec.expected;
    while (Date.now() <= deadline) {
      let matched = false;
      if (condition === "url_match") matched = location.href.includes(String(expected || ""));
      else if (condition === "text_appearance") matched = Boolean(document.body && document.body.innerText.includes(String(expected || "")));
      else if (condition === "dom_selector_state") {
        const selector = typeof expected === "object" && expected ? expected.selector : expected;
        try { matched = Boolean(document.querySelector(String(selector || ""))); } catch (_) {}
      } else if (condition === "element_state") {
        const ref = typeof expected === "object" && expected ? expected.element_ref : "";
        const element = elements.get(String(ref || ""));
        matched = Boolean(element && element.isConnected);
      }
      if (matched) return { matched: true, condition };
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("browser_timeout");
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message !== "object" || !String(message.type || "").startsWith("osia.")) return false;
    Promise.resolve().then(async () => {
      switch (message.type) {
        case "osia.challenge": return { document_nonce: documentNonce, ...safeLocation() };
        case "osia.inspect_page": return inspectPage(message.max_items);
        case "osia.inspect_forms": return inspectForms(message.max_fields);
        case "osia.activate": return activate(message.element_ref);
        case "osia.prepare_action": return prepareAction(message.element_ref);
        case "osia.fill": return fillField(message.field_ref, message.value);
        case "osia.wait": return waitCondition(message.wait_spec || {});
        default: throw new Error("unknown_operation");
      }
    }).then((result) => sendResponse({ ok: true, result })).catch((error) => {
      const code = /^[a-z][a-z0-9_]{1,63}$/.test(String(error && error.message || "")) ? error.message : "browser_operation_failed";
      sendResponse({ ok: false, error: { code } });
    });
    return true;
  });

  globalThis.__osiaPersonalBrowserAgent = { documentNonce };
})();
