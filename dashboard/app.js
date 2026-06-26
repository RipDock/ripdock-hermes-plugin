(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const Registry = window.__HERMES_PLUGINS__;
  if (!SDK || !Registry || typeof Registry.register !== "function") return;

  const React = SDK.React;
  const h = React.createElement;
  const components = SDK.components || {};
  const Card = components.Card || "section";
  const CardContent = components.CardContent || "div";
  const Badge = components.Badge || "span";

  const API_BASE = "/api/plugins/ripdock";
  const ICON_OPTIONS = [
    { value: "", label: "No icon", marker: "None" },
    { value: "🤖", label: "Robot" },
    { value: "🧠", label: "Brain" },
    { value: "🏠", label: "Home" },
    { value: "🛠️", label: "Tools" },
    { value: "💻", label: "Computer" },
    { value: "⚡", label: "Fast" },
    { value: "🔒", label: "Lock" },
    { value: "🚀", label: "Rocket" },
    { value: "☁️", label: "Cloud" },
    { value: "🧪", label: "Lab" },
    { value: "📱", label: "Phone" },
    { value: "🧭", label: "Compass" },
    { value: "✨", label: "Spark" },
    { value: "🧰", label: "Kit" },
    { value: "📡", label: "Signal" },
    { value: "🔴", label: "Red" },
    { value: "🟠", label: "Orange" },
    { value: "🟡", label: "Yellow" },
    { value: "🟢", label: "Green" },
    { value: "🔵", label: "Blue" },
    { value: "🟣", label: "Indigo" },
    { value: "🟪", label: "Violet" },
    { value: "⭐", label: "Star" },
  ];
  const ACCENT_COLORS = [
    { value: "", label: "No accent", marker: "None" },
    "#2563eb",
    "#0f766e",
    "#7c3aed",
    "#dc2626",
    "#ea580c",
    "#16a34a",
    "#0891b2",
    "#4f46e5",
  ];
  const TINT_COLORS = [
    "#ffffff",
    "#dbeafe",
    "#ccfbf1",
    "#dcfce7",
    "#fef3c7",
    "#fae8ff",
    "#ffe4e6",
    "#e0e7ff",
  ];

  function sessionHeaders() {
    const token = window.__HERMES_SESSION_TOKEN__;
    return token ? { "X-Hermes-Session-Token": token, Authorization: "Bearer " + token } : {};
  }

  async function api(path, options) {
    const response = await fetch(API_BASE + path, {
      credentials: "same-origin",
      ...options,
      headers: {
        accept: "application/json",
        ...(options && options.body ? { "content-type": "application/json" } : {}),
        ...sessionHeaders(),
        ...((options && options.headers) || {}),
      },
    });
    const text = await response.text();
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_error) {
        payload = { detail: text };
      }
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.message || "Request failed");
    }
    return payload;
  }

  function copy(value) {
    if (!value || !navigator.clipboard) return;
    navigator.clipboard.writeText(value).catch(function () {});
  }

  function Field(props) {
    const isNode = typeof props.value === "object" && props.value !== null;
    return h("div", { className: "ripdock-field" },
      h("span", { className: "ripdock-field-label" }, props.label),
      h("span", { className: "ripdock-field-value", title: isNode ? "" : (props.value || "") }, props.value || "—"),
      props.copy && props.value && !isNode
        ? h("button", { className: "ripdock-icon-button", type: "button", title: "Copy", onClick: props.onCopy || function () { copy(props.value); } }, props.copyLabel || "Copy")
        : null
    );
  }

  function SectionTitle(props) {
    return h("div", { className: "ripdock-section-title" },
      h("div", null,
        h("h2", null, props.title),
        props.description ? h("p", null, props.description) : null
      ),
      props.aside || null
    );
  }

  function SwatchPicker(props) {
    return h("div", { className: props.className || "ripdock-swatch-row", role: "group", "aria-label": props.label, "data-testid": props.testId || null },
      props.options.map(function (option) {
        const value = typeof option === "string" ? option : option.value;
        const label = typeof option === "string" ? option : option.label;
        const marker = typeof option === "string" ? option : (option.marker || option.value);
        const active = value === props.value;
        return h("button", {
          key: label + ":" + value,
          className: (active ? "ripdock-swatch active" : "ripdock-swatch") + (value === "" ? " none-swatch" : "") + (props.emoji && value === "" ? " no-icon" : ""),
          type: "button",
          title: label,
          "data-testid": props.testId ? props.testId + ".option." + String(label).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") : null,
          "aria-label": label,
          style: props.emoji || value === "" ? null : { background: value },
          onClick: function () { props.onChange(value); },
        }, props.emoji || value === "" ? marker : "");
      })
    );
  }

  function hasIconValue(value) {
    return ICON_OPTIONS.some(function (option) { return option.value === value; });
  }

  function Toast(props) {
    if (!props.toast || !props.toast.message) return null;
    return h("div", { className: "ripdock-toast-area", role: "status", "aria-live": "polite" },
      h("div", { className: "ripdock-toast " + (props.toast.tone || "") },
        h("span", null, props.toast.message),
        h("button", { className: "ripdock-toast-close", type: "button", title: "Dismiss", onClick: props.onClose }, "Dismiss")
      )
    );
  }

  function shortValue(value) {
    if (!value) return "—";
    const text = String(value);
    return text.length > 18 ? text.slice(0, 10) + "…" + text.slice(-6) : text;
  }

  function DeviceValue(props) {
    const value = props.value || "";
    return h("div", { className: "ripdock-device-value-row" },
      h("span", {
        className: "ripdock-device-value" + (props.full ? " ripdock-device-value-full" : ""),
        draggable: "false",
        onMouseDown: function (event) { event.preventDefault(); },
        title: value || "—"
      }, props.full ? (value || "—") : shortValue(value))
    );
  }

  function DeviceList(props) {
    const devices = props.devices || [];
    return h("div", { className: "ripdock-device-list", "data-testid": props.testId || null },
      devices.length === 0
        ? h("div", { className: "ripdock-empty", "data-testid": props.emptyTestId || null }, props.empty)
        : devices.map(function (device) {
          const key = device.deviceId || device.deviceFingerprint || JSON.stringify(device);
          const editing = props.editingLabel && props.editingLabel.deviceId === device.deviceId;
          const label = device.label || "";
          return h("article", { className: "ripdock-device-card", key: key, "data-testid": props.cardTestId || "dashboard.device.card", "data-device-id": device.deviceId || "", "data-device-fingerprint": device.deviceFingerprint || "" },
            h("div", { className: "ripdock-device-main" },
              h("div", { className: "ripdock-device-heading" },
                editing
                  ? h("div", { className: "ripdock-device-label-editor" },
                        h("input", {
                          type: "text",
                          value: props.editingLabel.value,
                          maxLength: 80,
                          "aria-label": "Device label",
                          "data-testid": "dashboard.device.label_input",
                        onChange: function (event) { props.onLabelChange(event.target.value); },
                        onKeyDown: function (event) {
                          if (event.key === "Escape") props.onCancelLabel();
                          if (event.key === "Enter") props.onSaveLabel(device);
                        }
                      }),
                      h("button", { type: "button", "data-testid": "dashboard.device.label_save_button", onClick: function () { props.onSaveLabel(device); }, disabled: props.labelSaving }, props.labelSaving ? "Saving..." : "Save"),
                      h("button", { type: "button", className: "ripdock-secondary", "data-testid": "dashboard.device.label_cancel_button", onClick: props.onCancelLabel, disabled: props.labelSaving }, "Cancel")
                    )
                  : h("div", { className: "ripdock-device-name" }, label || "Unnamed Device"),
                !editing && props.onStartLabel
                  ? h("button", { className: "ripdock-secondary ripdock-label-edit", type: "button", "data-testid": "dashboard.device.label_edit_button", onClick: function () { props.onStartLabel(device); }, disabled: !device.deviceId }, "Edit Label")
                  : null
              ),
              h("div", { className: "ripdock-device-grid" + (props.fingerprintOnly ? " ripdock-device-grid-fingerprint-only" : "") + (props.fullIdentifiers ? " ripdock-device-grid-full-identifiers" : "") },
                !props.fingerprintOnly ? h("div", { className: "ripdock-device-primary-field" },
                  h("span", { className: "ripdock-device-label" }, "Device ID"),
                  h(DeviceValue, { value: device.deviceId, full: props.fullIdentifiers })
                ) : null,
                h("div", { className: "ripdock-device-primary-field" + (props.fullIdentifiers ? " ripdock-device-fingerprint-field" : "") },
                  h("span", { className: "ripdock-device-label" }, "Fingerprint"),
                  h(DeviceValue, { value: device.deviceFingerprint, full: props.fingerprintOnly || props.fullIdentifiers })
                ),
                !props.fingerprintOnly ? h("div", { className: "ripdock-device-time-field" },
                  h("span", { className: "ripdock-device-label" }, props.timeLabel),
                  h("span", { className: "ripdock-device-meta" }, props.timeValue(device) || "—")
                ) : null,
                !props.fingerprintOnly ? h("div", { className: "ripdock-device-time-field" },
                  h("span", { className: "ripdock-device-label" }, props.extraTimeLabel || "Last seen"),
                  h("span", { className: "ripdock-device-meta" }, props.extraTimeValue ? (props.extraTimeValue(device) || "—") : (device.lastSeen || "—"))
                ) : null
              )
            ),
            h("div", { className: "ripdock-device-actions" },
              props.actions(device)
            )
          );
        })
    );
  }

  function RIPDOCKProtocolPage() {
    const React = SDK.React;
    const connectedToastShown = React.useRef(false);
    const [state, setState] = React.useState(null);
    const [toast, setToast] = React.useState(null);
    const [saving, setSaving] = React.useState(false);
    const [actionStatus, setActionStatus] = React.useState({});
    const [copiedKey, setCopiedKey] = React.useState("");
    const [publicURL, setPublicURL] = React.useState("");
    const [publicURLError, setPublicURLError] = React.useState("");
    const publicURLDirtyRef = React.useRef(false);
    const [editingLabel, setEditingLabel] = React.useState(null);
    const agentDirtyRef = React.useRef({});
    const [agentDrafts, setAgentDrafts] = React.useState({});
    const [agentDirty, setAgentDirty] = React.useState({});
    const [showDisabledAgents, setShowDisabledAgents] = React.useState(false);
    const [runtimeDraft, setRuntimeDraft] = React.useState({
      displayName: "",
      icon: "",
      accentColor: "#2563eb",
      backgroundColor: "#ffffff",
    });
    const runtimeDirtyRef = React.useRef(false);
    const [runtimeDirty, setRuntimeDirty] = React.useState(false);

    function showToast(tone, message) {
      setToast({ tone: tone, message: message, id: Date.now() });
    }

    function setAction(key, status) {
      setActionStatus(function (current) {
        return { ...current, [key]: status };
      });
      if (status === "success") {
        setTimeout(function () {
          setActionStatus(function (current) {
            if (current[key] !== "success") return current;
            return { ...current, [key]: "idle" };
          });
        }, 1800);
      }
    }

    function actionLabel(key, idle, loading, success) {
      if (actionStatus[key] === "loading") return loading;
      if (actionStatus[key] === "success") return success;
      if (actionStatus[key] === "error") return "Error";
      return idle;
    }

    function isActionLoading(key) {
      return actionStatus[key] === "loading";
    }

    function copyValue(key, value) {
      copy(value);
      setCopiedKey(key);
      setTimeout(function () {
        setCopiedKey(function (current) {
          return current === key ? "" : current;
        });
      }, 1400);
    }

    function copyLabel(key, fallback) {
      return copiedKey === key ? "✓ Copied" : fallback;
    }

    function agentDraftFromRecord(agent) {
      return {
        agentId: agent.agent_id || agent.agentId || "",
        displayName: agent.display_name || agent.displayName || "",
        icon: hasIconValue(agent.icon) ? agent.icon : "",
        accentColor: agent.accent_color || agent.accentColor || "#2563eb",
        backgroundColor: agent.background_color || agent.backgroundColor || "#dbeafe",
        sortOrder: Number.isInteger(agent.sort_order) ? agent.sort_order : (Number.isInteger(agent.sortOrder) ? agent.sortOrder : 0),
        enabled: agent.enabled !== false,
      };
    }

    function runtimeDraftFromRecord(next) {
      const metadata = (next && next.runtimeMetadata) || {};
      const identity = (next && next.runtimeIdentity) || {};
      return {
        displayName: metadata.displayName || identity.displayName || "Hermes",
        icon: hasIconValue(metadata.icon) ? metadata.icon : "",
        accentColor: metadata.accentColor || "#2563eb",
        backgroundColor: metadata.backgroundColor || "#ffffff",
      };
    }

    function hydrateRuntime(next) {
      if (runtimeDirtyRef.current) return;
      setRuntimeDraft(runtimeDraftFromRecord(next));
    }

    function hydratePublicURL(next) {
      if (publicURLDirtyRef.current) return;
      setPublicURL((next.publicURL && (next.publicURL.configured || next.publicURL.active)) || "");
    }

    function hydrateAgents(next) {
      const agents = (next && next.runtimeAgents) || [];
      setAgentDrafts(function (current) {
        const nextDrafts = {};
        agents.forEach(function (agent) {
          const draft = agentDraftFromRecord(agent);
          if (!draft.agentId) return;
          nextDrafts[draft.agentId] = agentDirtyRef.current[draft.agentId] ? (current[draft.agentId] || draft) : draft;
        });
        return nextDrafts;
      });
    }

    function refresh() {
      return api("/state")
        .then(function (next) {
          setState(next);
          hydratePublicURL(next);
          hydrateRuntime(next);
          hydrateAgents(next);
          if (next.backend && next.backend.runtimeAdminAvailable && !connectedToastShown.current) {
            connectedToastShown.current = true;
            showToast("ok", "Runtime Connected");
          }
        })
        .catch(function (err) { showToast("error", err.message); });
    }

    React.useEffect(function () {
      refresh();
      const refreshTimer = setInterval(refresh, 10000);
      return function () {
        clearInterval(refreshTimer);
      };
    }, []);

    React.useEffect(function () {
      if (!toast) return undefined;
      const delay = toast.tone === "error" ? 10000 : 4000;
      const timer = setTimeout(function () { setToast(null); }, delay);
      return function () { clearTimeout(timer); };
    }, [toast]);

    function savePublicURL() {
      setSaving(true);
      setPublicURLError("");
      setAction("savePublicURL", "loading");
      api("/public-url", { method: "POST", body: JSON.stringify({ publicURL: publicURL }) })
        .then(function (payload) {
          showToast("ok", "Public RIPDOCK URL saved.");
          setAction("savePublicURL", "success");
          setState(payload.state || state);
          publicURLDirtyRef.current = false;
          hydratePublicURL(payload.state || { publicURL: { configured: payload.publicURL || "" } });
        })
        .catch(function (err) {
          setAction("savePublicURL", "error");
          setPublicURLError(err.message);
        })
        .finally(function () { setSaving(false); });
    }

    function updateAgentDraft(agentId, key, value) {
      agentDirtyRef.current = { ...agentDirtyRef.current, [agentId]: true };
      setAgentDirty(function (current) { return { ...current, [agentId]: true }; });
      setAgentDrafts(function (current) {
        const draft = current[agentId] || { agentId: agentId };
        return { ...current, [agentId]: { ...draft, [key]: value } };
      });
    }

    function updateRuntimeDraft(key, value) {
      runtimeDirtyRef.current = true;
      setRuntimeDirty(true);
      setRuntimeDraft(function (current) {
        return { ...current, [key]: value };
      });
    }

    function saveRuntimeMetadata() {
      setSaving(true);
      setAction("saveRuntimeMetadata", "loading");
      api("/metadata", { method: "POST", body: JSON.stringify(runtimeDraft) })
        .then(function (payload) {
          showToast("ok", "Runtime metadata saved.");
          setAction("saveRuntimeMetadata", "success");
          setState(payload.state || state);
          runtimeDirtyRef.current = false;
          setRuntimeDirty(false);
          setRuntimeDraft(runtimeDraftFromRecord(payload.state || payload));
        })
        .catch(function (err) {
          setAction("saveRuntimeMetadata", "idle");
          showToast("error", err.message);
        })
        .finally(function () { setSaving(false); });
    }

    function saveAgent(agentId) {
      const draft = agentDrafts[agentId];
      if (!draft) return;
      const key = "saveAgent:" + agentId;
      setSaving(true);
      setAction(key, "loading");
      api("/agents/" + encodeURIComponent(agentId) + "/metadata", { method: "POST", body: JSON.stringify(draft) })
        .then(function (payload) {
          showToast("ok", "Agent metadata saved.");
          setAction(key, "success");
          setState(payload.state || state);
          agentDirtyRef.current = { ...agentDirtyRef.current, [agentId]: false };
          setAgentDirty(function (current) { return { ...current, [agentId]: false }; });
          hydrateAgents(payload.state || { runtimeAgents: payload.runtimeAgents || [] });
        })
        .catch(function (err) {
          setAction(key, "idle");
          showToast("error", err.message);
        })
        .finally(function () { setSaving(false); });
    }

    function saveAgentEnabled(agentId, enabled, draft) {
      const nextDraft = { ...(draft || agentDrafts[agentId] || { agentId: agentId }), enabled: enabled };
      const key = "toggleAgent:" + agentId;
      setAgentDrafts(function (current) {
        return { ...current, [agentId]: nextDraft };
      });
      setAction(key, "loading");
      api("/agents/" + encodeURIComponent(agentId) + "/metadata", { method: "POST", body: JSON.stringify(nextDraft) })
        .then(function (payload) {
          showToast("ok", enabled ? "Agent enabled." : "Agent disabled.");
          setAction(key, "success");
          setState(payload.state || state);
          agentDirtyRef.current = { ...agentDirtyRef.current, [agentId]: false };
          setAgentDirty(function (current) { return { ...current, [agentId]: false }; });
          hydrateAgents(payload.state || { runtimeAgents: payload.runtimeAgents || [] });
        })
        .catch(function (err) {
          setAction(key, "idle");
          hydrateAgents(state || { runtimeAgents: agents });
          showToast("error", err.message);
        });
    }

    function deviceAction(event, action) {
      const button = event && event.currentTarget && event.currentTarget.closest
        ? event.currentTarget.closest("[data-device-id]")
        : null;
      const deviceId = button && button.dataset ? button.dataset.deviceId : "";
      const key = "device:" + action + ":" + deviceId;
      if (!deviceId) {
        showToast("error", "Missing Device ID.");
        return;
      }
      setAction(key, "loading");
      const requestOptions = { method: "POST" };
      if (action !== "revoke") {
        requestOptions.body = JSON.stringify({ deviceId: deviceId, action: action });
      }
      api("/devices/" + encodeURIComponent(deviceId) + "/" + action, requestOptions)
        .then(function (payload) {
          const defaultMessage = action.charAt(0).toUpperCase() + action.slice(1) + " completed.";
          showToast("ok", payload.message || defaultMessage);
          setAction(key, "success");
          refresh();
        })
        .catch(function (err) {
          setAction(key, "idle");
          showToast("error", err.message);
        });
    }

    function startLabelEdit(device) {
      setEditingLabel({ deviceId: device.deviceId, value: device.label || "" });
    }

    function updateLabelDraft(value) {
      setEditingLabel(function (current) {
        if (!current) return current;
        return { ...current, value: value };
      });
    }

    function cancelLabelEdit() {
      setEditingLabel(null);
    }

    function saveDeviceLabel(device) {
      if (!editingLabel || !device.deviceId) return;
      const key = "device:label:" + device.deviceId;
      setAction(key, "loading");
      api("/devices/" + encodeURIComponent(device.deviceId) + "/label", {
        method: "POST",
        body: JSON.stringify({ label: editingLabel.value }),
      })
        .then(function (payload) {
          setState(payload.state || state);
          setEditingLabel(null);
          setAction(key, "success");
          showToast("ok", payload.label ? "Device label saved." : "Device label cleared.");
        })
        .catch(function (err) {
          setAction(key, "idle");
          showToast("error", err.message);
        });
    }

    const runtime = (state && state.runtimeIdentity) || {};
    const publicInfo = (state && state.publicURL) || {};
    const backend = (state && state.backend) || {};
    const agents = (state && state.runtimeAgents) || [];
    const enabledAgents = agents.filter(function (agent) { return agent.enabled !== false; });
    const disabledAgents = agents.filter(function (agent) { return agent.enabled === false; });

    function renderAgentCard(agent) {
      const agentId = agent.agent_id || agent.agentId;
      const draft = agentDrafts[agentId] || agentDraftFromRecord(agent);
      const actionKey = "saveAgent:" + agentId;
      const toggleKey = "toggleAgent:" + agentId;
      return h("article", { className: "ripdock-agent-card" + (agent.enabled === false ? " disabled" : ""), key: agentId, "data-testid": "dashboard.agent.card", "data-agent-id": agentId },
        h("div", { className: "ripdock-agent-card-header" },
          h("label", { className: "ripdock-switch-row" },
            h("input", { type: "checkbox", checked: draft.enabled !== false, disabled: isActionLoading(toggleKey), "data-testid": "dashboard.agent.enabled", onChange: function (event) { saveAgentEnabled(agentId, event.target.checked, draft); } }),
            h("span", { className: "ripdock-switch", "aria-hidden": "true" }),
            h("span", null, "Enabled")
          )
        ),
        h("div", {
          className: "ripdock-runtime-preview",
          style: {
            borderColor: draft.accentColor || "#2563eb",
            background: draft.backgroundColor || "#dbeafe",
          }
        },
          h("div", { className: draft.icon ? "ripdock-preview-icon" : "ripdock-preview-icon no-icon" }, draft.icon || "None"),
          h("div", { className: "ripdock-preview-body" },
            h("div", { className: "ripdock-preview-top" },
              h("strong", null, draft.displayName || agentId),
              h("span", { className: "ripdock-trust-badge" }, draft.enabled === false ? "Disabled" : "Enabled")
            ),
            h("div", { className: "ripdock-preview-fingerprint" },
              h("span", null, "ID"),
              h("code", null, agentId)
            )
          )
        ),
        h("div", { className: "ripdock-agent-editor" },
          h("label", { className: "ripdock-single-field" }, "Display Name", h("input", { value: draft.displayName, "data-testid": "dashboard.agent.display_name", onChange: function (event) { updateAgentDraft(agentId, "displayName", event.target.value); } })),
          h("div", { className: "ripdock-picker-group" },
            h("span", { className: "ripdock-picker-label" }, "Icon"),
            h(SwatchPicker, { label: "Agent Icon", testId: "dashboard.agent.icon_picker", emoji: true, options: ICON_OPTIONS, value: draft.icon, onChange: function (value) { updateAgentDraft(agentId, "icon", value); } })
          ),
          h("div", { className: "ripdock-picker-group" },
            h("span", { className: "ripdock-picker-label" }, "Accent Color"),
            h(SwatchPicker, { label: "Agent Accent Color", testId: "dashboard.agent.accent_picker", options: ACCENT_COLORS, value: draft.accentColor, onChange: function (value) { updateAgentDraft(agentId, "accentColor", value); } })
          ),
          h("div", { className: "ripdock-picker-group" },
            h("span", { className: "ripdock-picker-label" }, "Background"),
            h(SwatchPicker, { label: "Agent Background", testId: "dashboard.agent.background_picker", options: TINT_COLORS, value: draft.backgroundColor, onChange: function (value) { updateAgentDraft(agentId, "backgroundColor", value); } })
          ),
          h("button", { type: "button", "data-testid": "dashboard.agent.save_button", className: isActionLoading(actionKey) ? "ripdock-loading" : "", onClick: function () { saveAgent(agentId); }, disabled: saving || !agentDirty[agentId] }, actionLabel(actionKey, "Save Agent", "Saving...", "Saved"))
        )
      );
    }

    return h("div", { className: "ripdock-plugin-root ripdock-dashboard", "data-testid": "dashboard.root" },
      h("header", { className: "ripdock-header" },
        h(Badge, { tone: "outline", className: "ripdock-badge" }, backend.runtimeAdminAvailable ? "Runtime Connected" : "Dashboard Fallback")
      ),

      h(Toast, { toast: toast, onClose: function () { setToast(null); } }),

      h("div", { className: "ripdock-grid" },
        h(Card, { className: "ripdock-card ripdock-wide", "data-testid": "dashboard.runtime_identity" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, {
              title: "Runtime Identity"
            }),
            h("div", { className: "ripdock-fields" },
              h(Field, { label: "Runtime ID", value: runtime.runtimeId, copy: true, copyLabel: copyLabel("runtimeId", "Copy"), onCopy: function () { copyValue("runtimeId", runtime.runtimeId); } }),
              h(Field, { label: "Fingerprint", value: runtime.publicKeyFingerprint, copy: true, copyLabel: copyLabel("fingerprint", "Copy"), onCopy: function () { copyValue("fingerprint", runtime.publicKeyFingerprint); } }),
              h(Field, { label: "Protocol version", value: runtime.protocolVersion })
            )
          )
        ),

        h(Card, { className: "ripdock-card", "data-testid": "dashboard.public_url" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, {
              title: "Public RIPDOCK URL",
              description: "This is the HTTPS URL RIPDOCK devices use to reach this Hermes runtime."
            }),
            h("div", { className: "ripdock-form-row ripdock-public-url-row" },
              h("input", {
                value: publicURL,
                placeholder: "https://runtime.example.com",
                "data-testid": "dashboard.public_url.input",
                onChange: function (event) {
                  setPublicURL(event.target.value);
                  publicURLDirtyRef.current = true;
                  setPublicURLError("");
                  if (actionStatus.savePublicURL === "error") setAction("savePublicURL", "idle");
                }
              }),
              h("button", { className: "ripdock-secondary", type: "button", title: "Copy", onClick: function () { copyValue("configuredPublicURL", publicURL || publicInfo.configured || publicInfo.active || ""); }, disabled: !(publicURL || publicInfo.configured || publicInfo.active) }, copyLabel("configuredPublicURL", "Copy")),
              h("button", { type: "button", "data-testid": "dashboard.public_url.save_button", className: isActionLoading("savePublicURL") ? "ripdock-loading" : "", onClick: savePublicURL, disabled: saving }, actionLabel("savePublicURL", "Save", "Saving...", "Saved"))
            ),
            publicURLError ? h("div", { className: "ripdock-inline-error", role: "alert" }, publicURLError) : null
          )
        ),

        h(Card, { className: "ripdock-card ripdock-wide", "data-testid": "dashboard.runtime_metadata" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, {
              title: "Runtime Metadata",
              description: "Configure the Runtime display and theme metadata used by RIPDOCK Apps."
            }),
            h("div", {
              className: "ripdock-runtime-preview",
              style: {
                borderColor: runtimeDraft.accentColor || "#2563eb",
                background: runtimeDraft.backgroundColor || "#ffffff",
              },
              "data-testid": "dashboard.runtime.preview"
            },
              h("div", { className: runtimeDraft.icon ? "ripdock-preview-icon" : "ripdock-preview-icon no-icon" }, runtimeDraft.icon || "None"),
              h("div", { className: "ripdock-preview-body" },
                h("div", { className: "ripdock-preview-top" },
                  h("strong", { "data-testid": "dashboard.runtime.preview_name" }, runtimeDraft.displayName || "Hermes")
                ),
                h("div", { className: "ripdock-preview-url" }, publicInfo.configured || publicInfo.active || "Runtime URL not configured"),
                h("div", { className: "ripdock-preview-fingerprint" },
                  h("span", null, "Runtime ID"),
                  h("code", null, runtime.runtimeId || "—")
                )
              )
            ),
            h("div", { className: "ripdock-agent-editor" },
              h("label", { className: "ripdock-single-field" }, "Display Name", h("input", { value: runtimeDraft.displayName, "data-testid": "dashboard.runtime.display_name", onChange: function (event) { updateRuntimeDraft("displayName", event.target.value); } })),
              h("div", { className: "ripdock-picker-group" },
                h("span", { className: "ripdock-picker-label" }, "Icon"),
                h(SwatchPicker, { label: "Runtime Icon", testId: "dashboard.runtime.icon_picker", emoji: true, options: ICON_OPTIONS, value: runtimeDraft.icon, onChange: function (value) { updateRuntimeDraft("icon", value); } })
              ),
              h("div", { className: "ripdock-picker-group" },
                h("span", { className: "ripdock-picker-label" }, "Accent Color"),
                h(SwatchPicker, { label: "Runtime Accent Color", testId: "dashboard.runtime.accent_picker", options: ACCENT_COLORS, value: runtimeDraft.accentColor, onChange: function (value) { updateRuntimeDraft("accentColor", value); } })
              ),
              h("div", { className: "ripdock-picker-group" },
                h("span", { className: "ripdock-picker-label" }, "Background"),
                h(SwatchPicker, { label: "Runtime Background", testId: "dashboard.runtime.background_picker", options: TINT_COLORS, value: runtimeDraft.backgroundColor, onChange: function (value) { updateRuntimeDraft("backgroundColor", value); } })
              ),
              h("button", { type: "button", "data-testid": "dashboard.runtime.save_button", className: isActionLoading("saveRuntimeMetadata") ? "ripdock-loading" : "", onClick: saveRuntimeMetadata, disabled: saving || !runtimeDirty }, actionLabel("saveRuntimeMetadata", "Save Runtime", "Saving...", "Saved"))
            )
          )
        ),

        h(Card, { className: "ripdock-card ripdock-wide", "data-testid": "dashboard.agents" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, {
              title: "Agents",
              description: "Profiles discovered from the Runtime. Configure Agent display and theme metadata for RIPDOCK.",
              aside: disabledAgents.length
                ? h("button", { type: "button", className: "ripdock-secondary", "data-testid": "dashboard.agents.show_disabled_button", onClick: function () { setShowDisabledAgents(!showDisabledAgents); } }, showDisabledAgents ? "Hide Disabled" : "Show Disabled")
                : null
            }),
            agents.length === 0
              ? h("div", { className: "ripdock-empty" }, "No Agents advertised.")
              : h(React.Fragment, null,
                  enabledAgents.length
                    ? h("div", { className: "ripdock-agent-list" }, enabledAgents.map(renderAgentCard))
                    : h("div", { className: "ripdock-empty" }, "No enabled Agents."),
                  showDisabledAgents && disabledAgents.length
                    ? h("div", { className: "ripdock-agent-list ripdock-disabled-agent-list" }, disabledAgents.map(renderAgentCard))
                    : null
                )
          )
        ),

        h(Card, { className: "ripdock-card ripdock-wide", "data-testid": "dashboard.pending_devices" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, { title: "Pending Devices" }),
            h(DeviceList, {
              devices: (state && state.pendingDevices) || [],
              empty: "No pending Devices.",
              testId: "dashboard.pending_devices.list",
              emptyTestId: "dashboard.pending_devices.empty",
              cardTestId: "dashboard.pending_devices.card",
              timeLabel: "Claimed",
              extraTimeLabel: "Expires",
              timeValue: function (device) { return device.claimedTime; },
              extraTimeValue: function (device) { return device.expiresAt; },
              fingerprintOnly: true,
              copyLabel: copyLabel,
              onCopy: copyValue,
              editingLabel: editingLabel,
              onStartLabel: startLabelEdit,
              onLabelChange: updateLabelDraft,
              onCancelLabel: cancelLabelEdit,
              onSaveLabel: saveDeviceLabel,
              labelSaving: editingLabel && isActionLoading("device:label:" + editingLabel.deviceId),
              actions: function (device) {
                const approveKey = "device:approve:" + device.deviceId;
                const rejectKey = "device:reject:" + device.deviceId;
                return [
                  h("button", { key: "approve", type: "button", className: isActionLoading(approveKey) ? "ripdock-loading" : "", "data-testid": "dashboard.pending_devices.approve_button", "data-device-id": device.deviceId, "data-device-fingerprint": device.deviceFingerprint || "", onClick: function (event) { deviceAction(event, "approve"); }, disabled: !device.deviceId || isActionLoading(approveKey) }, actionLabel(approveKey, "Approve", "Approving...", "Approved")),
                  h("button", { key: "reject", className: "ripdock-danger " + (isActionLoading(rejectKey) ? "ripdock-loading" : ""), type: "button", "data-testid": "dashboard.pending_devices.reject_button", "data-device-id": device.deviceId, "data-device-fingerprint": device.deviceFingerprint || "", onClick: function (event) { deviceAction(event, "reject"); }, disabled: !device.deviceId || isActionLoading(rejectKey) }, actionLabel(rejectKey, "Reject / Delete", "Rejecting...", "Rejected"))
                ];
              }
            })
          )
        ),

        h(Card, { className: "ripdock-card ripdock-wide", "data-testid": "dashboard.trusted_devices" },
          h(CardContent, { className: "ripdock-card-content" },
            h(SectionTitle, { title: "Trusted Devices" }),
            h(DeviceList, {
              devices: (state && state.trustedDevices) || [],
              empty: "No trusted Devices.",
              testId: "dashboard.trusted_devices.list",
              emptyTestId: "dashboard.trusted_devices.empty",
              cardTestId: "dashboard.trusted_devices.card",
              timeLabel: "Approved",
              timeValue: function (device) { return device.approvedTime; },
              fullIdentifiers: true,
              copyLabel: copyLabel,
              onCopy: copyValue,
              editingLabel: editingLabel,
              onStartLabel: startLabelEdit,
              onLabelChange: updateLabelDraft,
              onCancelLabel: cancelLabelEdit,
              onSaveLabel: saveDeviceLabel,
              labelSaving: editingLabel && isActionLoading("device:label:" + editingLabel.deviceId),
              actions: function (device) {
                const revokeKey = "device:revoke:" + device.deviceId;
                return h("button", { type: "button", className: "ripdock-danger " + (isActionLoading(revokeKey) ? "ripdock-loading" : ""), "data-testid": "dashboard.trusted_devices.revoke_button", "data-device-id": device.deviceId, "data-device-fingerprint": device.deviceFingerprint || "", onClick: function (event) { deviceAction(event, "revoke"); }, disabled: !device.deviceId || isActionLoading(revokeKey) }, actionLabel(revokeKey, "Revoke / Delete", "Revoking...", "Revoked"));
              }
            })
          )
        )
      )
    );
  }

  Registry.register("ripdock", RIPDOCKProtocolPage);
})();
