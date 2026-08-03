/**
 * ORVIBO 门锁卡片（纯原生 JS，无第三方依赖）。
 *
 * 功能：
 *  - 门锁状态总览（锁状态/门磁/电池/最近截图）
 *  - 下发临时密码（type/minutes/number/phone/name，可选短信）
 *  - 临时密码列表管理（查看/删除/过期状态）
 *
 * 用法（Lovelace 卡片）：
 *   type: custom:orvibo-door-lock-card
 *   device_id: <your-door-lock-device-id>   # 可选，留空自动选第一把门锁
 */

const ORVIBO_PREFIX = "orvibohomebridge_";

class OrviboDoorLockCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._entities = {};   // device_id -> {kind -> entity_id}
    this._deviceName = "";
    this._tempResult = "";
    this._tempError = "";
    this._entitiesLoaded = false;
    this._listOpen = false;
    this._lastListAt = 0;
    this._listLoaded = false;
    this.attachShadow({ mode: "open" });
  }

  static LIST_THROTTLE_MS = 60000;

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._loadEntities();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entitiesLoaded) {
      this._loadEntities();
    } else {
      this._render();
    }
  }

  getCardSize() {
    return 6;
  }

  async _loadEntities() {
    if (!this._hass) return;
    try {
      const entities = await this._hass.callWS({ type: "config/entity_registry/list" });
      const devices = await this._hass.callWS({ type: "config/device_registry/list" });
      const lockDevices = new Map();
      for (const dev of devices) {
        const ids = (dev.identifiers || [])
          .map((x) => (Array.isArray(x) ? x[1] : x) || "")
          .join(",");
        const model = String(dev.model || "").toLowerCase();
        if (
          ids.includes("w-") ||
          model.includes("lock") ||
          model.includes("orvibo")
        ) {
          lockDevices.set(dev.id, { name: dev.name || "门锁", deviceId: ids });
        }
      }
      const byDevice = {};
      for (const ent of entities) {
        if (!ent.unique_id || !ent.unique_id.startsWith(ORVIBO_PREFIX)) continue;
        if (!ent.device_id) continue;
        const dev = lockDevices.get(ent.device_id);
        if (!dev) continue;
        const uid = ent.unique_id;
        let kind = "unknown";
        if (uid.includes("door_lock_state")) kind = "lock_state";
        else if (uid.includes("door_lock_door")) kind = "door";
        else if (uid.includes("dry_battery")) kind = "dry_battery";
        else if (uid.includes("lithium_battery")) kind = "lithium_battery";
        else if (uid.includes("door_lock_unlock")) kind = "unlock";
        else if (uid.includes("temp_password")) kind = "temp_password";
        else if (uid.includes("camera")) kind = "camera";
        else if (uid.includes("doorbell")) kind = "doorbell";
        byDevice[dev.deviceId] = byDevice[dev.deviceId] || { name: dev.name, entities: {} };
        byDevice[dev.deviceId].entities[kind] = ent.entity_id;
      }
      const requested = this._config.device_id;
      const candidates = Object.keys(byDevice);
      const deviceId = requested && byDevice[requested] ? requested : candidates[0];
      if (deviceId && byDevice[deviceId]) {
        this._deviceId = deviceId;
        this._deviceName = byDevice[deviceId].name;
        this._entities = byDevice[deviceId].entities;
        this._entitiesLoaded = true;
      }
    } catch (e) {
      console.error("ORVIBO card: 加载实体失败", e);
    }
    this._render();
  }

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return st ? st.state : null;
  }

  _attr(entityId, key) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return st && st.attributes ? st.attributes[key] : null;
  }

  _fmtTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  _render() {
    const root = this.shadowRoot;
    if (!this._hass) {
      root.innerHTML = "<ha-card style='padding:16px'>ORVIBO 门锁卡片</ha-card>";
      return;
    }
    if (!this._deviceId) {
      root.innerHTML = "<ha-card style='padding:16px'>未找到门锁设备，请配置 device_id 或确认集成已加载</ha-card>";
      return;
    }
    const e = this._entities;
    const lockState = this._state(e.lock_state) || "-";
    const door = this._state(e.door);
    const dryBattery = this._state(e.dry_battery);
    const lithiumBattery = this._state(e.lithium_battery);
    const tempPwd = this._state(e.temp_password);
    const lockStateLabel = { locked: "已上锁", unlocked: "未上锁", inside_locked: "门内已反锁", abnormal: "异常" }[lockState] || lockState;
    const doorLabel = door === "on" ? "开" : door === "off" ? "关" : (door || "-");
    const batteryLabel =
      dryBattery != null && dryBattery !== "unknown"
        ? `${dryBattery}%`
        : "-";
    const lithiumLabel =
      lithiumBattery != null && lithiumBattery !== "unknown"
        ? `${lithiumBattery}%`
        : "-";
    const cameraEntity = e.camera;
    const cameraState = cameraEntity ? this._hass.states[cameraEntity] : null;
    const cameraToken =
      cameraState && cameraState.attributes
        ? cameraState.attributes.access_token
        : "";
    const cameraUrl =
      cameraEntity && cameraToken
        ? `/api/camera_proxy/${cameraEntity}?token=${encodeURIComponent(cameraToken)}`
        : "";

    root.innerHTML = `
      <ha-card>
        <div class="header">
          <div>
            <div class="title">🔒 ${this._escapeHtml(this._deviceName)}</div>
            <div class="subtitle">ORVIBO 门锁 · 状态总览</div>
          </div>
          <div class="lock-badge ${lockState}">${this._escapeHtml(lockStateLabel)}</div>
        </div>
        <div class="stats">
          <div class="stat"><span class="stat-label">门磁</span><span class="stat-value">${this._escapeHtml(doorLabel)}</span></div>
          <div class="stat"><span class="stat-label">干电池</span><span class="stat-value">${this._escapeHtml(batteryLabel)}</span></div>
          <div class="stat"><span class="stat-label">锂电池</span><span class="stat-value">${this._escapeHtml(lithiumLabel)}</span></div>
          <div class="stat"><span class="stat-label">最近临时密码</span><span class="stat-value">${this._escapeHtml(tempPwd || "无")}</span></div>
        </div>
        ${cameraUrl ? `<div class="snapshot"><img src="${cameraUrl}" alt="门锁截图" /></div>` : ""}
        <div class="section">
          <div class="section-title">下发临时密码</div>
          <div class="form">
            <label>类型
              <select id="ov-tp-type">
                <option value="2">临时（时长+次数）</option>
                <option value="1">限时（开始+结束）</option>
              </select>
            </label>
            <label>时长（分钟）
              <input id="ov-tp-minutes" type="number" value="1440" min="1" />
            </label>
            <label>次数
              <input id="ov-tp-number" type="number" value="1" min="0" />
            </label>
            <label>手机号（短信通知，可选）
              <input id="ov-tp-phone" type="text" placeholder="13800138000" />
            </label>
            <label>名称（可选）
              <input id="ov-tp-name" type="text" placeholder="访客" />
            </label>
            <div class="actions">
              <ha-button raised id="ov-tp-grant">生成临时密码</ha-button>
            </div>
            ${this._tempError ? `<div class="error">${this._escapeHtml(this._tempError)}</div>` : ""}
            ${this._tempResult ? `<div class="result">临时密码：<b>${this._escapeHtml(this._tempResult)}</b></div>` : ""}
          </div>
        </div>
        <div class="section">
          <div class="section-title toggle" id="ov-tp-toggle">
            <span>${this._listOpen ? "▾" : "▸"} 临时密码管理</span>
            <ha-button id="ov-tp-refresh" size="small">刷新</ha-button>
          </div>
          <div id="ov-tp-list" class="tp-list" style="${this._listOpen ? "" : "display:none"}">
            ${this._listOpen ? "加载中..." : ""}
          </div>
        </div>
      </ha-card>
      <style>
        ha-card { padding: 16px; font-size: 14px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 600; }
        .subtitle { color: var(--secondary-text-color); font-size: 12px; }
        .lock-badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; background: var(--primary-color); color: var(--text-primary-color); }
        .lock-badge.unlocked { background: #43a047; }
        .lock-badge.abnormal { background: #e53935; }
        .stats { display: flex; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
        .stat { background: var(--card-background-color, #f5f5f5); border-radius: 8px; padding: 8px 12px; flex: 1; min-width: 90px; }
        .stat-label { display: block; color: var(--secondary-text-color); font-size: 11px; }
        .stat-value { font-weight: 600; }
        .snapshot img { width: 100%; border-radius: 8px; margin-bottom: 10px; max-height: 220px; object-fit: cover; }
        .section { border-top: 1px solid var(--divider-color); padding-top: 12px; margin-top: 12px; }
        .section-title { font-weight: 600; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
        .toggle { cursor: pointer; user-select: none; }
        .form { display: grid; gap: 8px; }
        .form label { display: grid; grid-template-columns: 110px 1fr; align-items: center; gap: 8px; }
        .form input, .form select { width: 100%; box-sizing: border-box; }
        .actions { margin-top: 8px; }
        .error { color: #e53935; margin-top: 8px; }
        .result { color: var(--primary-color); margin-top: 8px; font-size: 15px; }
        .tp-list { display: grid; gap: 6px; }
        .tp-item { display: flex; justify-content: space-between; align-items: center; background: var(--card-background-color, #f5f5f5); border-radius: 8px; padding: 8px 12px; }
        .tp-item .pwd { font-weight: 700; font-size: 16px; letter-spacing: 2px; }
        .tp-item .meta { color: var(--secondary-text-color); font-size: 12px; }
        .tp-item.expired { opacity: 0.5; }
      </style>
    `;

    root.querySelector("#ov-tp-grant").addEventListener("click", () => this._grant());
    root.querySelector("#ov-tp-toggle").addEventListener("click", (e) => {
      if (e.target.closest("#ov-tp-refresh")) return;
      this._listOpen = !this._listOpen;
      this._render();
    });
    root.querySelector("#ov-tp-refresh").addEventListener("click", () => {
      this._lastListAt = 0;
      this._listLoaded = false;
      this._loadList();
    });
    if (this._listOpen) {
      this._loadList();
    }
  }

  async _grant() {
    const root = this.shadowRoot;
    this._tempError = "";
    this._tempResult = "";
    const data = {
      device_id: this._deviceId,
      type: Number(root.querySelector("#ov-tp-type").value),
      minutes: Number(root.querySelector("#ov-tp-minutes").value || 1440),
      number: Number(root.querySelector("#ov-tp-number").value || 0),
      phone: root.querySelector("#ov-tp-phone").value.trim(),
      name: root.querySelector("#ov-tp-name").value.trim(),
    };
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibohomebridge",
        service: "grant_temp_password",
        service_data: data,
        return_response: true,
      });
      const res = result && result.response;
      if (res && res.error) {
        this._tempError = res.error;
      } else if (res && res.password) {
        this._tempResult = res.password;
      }
    } catch (e) {
      this._tempError = e.message || String(e);
    }
    this._lastListAt = 0;
    this._listLoaded = false;
    this._render();
    if (this._listOpen) {
      this._loadList();
    }
  }

  async _loadList() {
    const root = this.shadowRoot;
    if (!root.querySelector("#ov-tp-list")) return;
    const now = Date.now();
    if (
      this._listLoaded &&
      now - this._lastListAt < OrviboDoorLockCard.LIST_THROTTLE_MS
    ) {
      return; // 节流：60 秒内不重复拉取
    }
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibohomebridge",
        service: "list_temp_passwords",
        service_data: { device_id: this._deviceId },
        return_response: true,
      });
      const res = result && result.response;
      const records = (res && res[this._deviceId]) || [];
      console.log("ORVIBO list debug:", {
        deviceId: this._deviceId,
        res,
        count: records.length,
      });
      // 异步期间 DOM 可能被 _render 重建，必须重新查询最新节点
      const listEl = this.shadowRoot.querySelector("#ov-tp-list");
      if (!listEl) return;
      this._listLoaded = true;
      this._lastListAt = Date.now();
      if (!records.length) {
        listEl.innerHTML = "<div class='meta'>暂无临时密码</div>";
        return;
      }
      listEl.innerHTML = records
        .map(
          (r) => `
            <div class="tp-item ${r.expired ? "expired" : ""}">
              <div>
                <div class="pwd">${this._escapeHtml(r.password)}</div>
                <div class="meta">#${r.authorized_id} · ${r.name || "临时用户"} · ${this._fmtTs(r.end_time || r.start_time)} · ${r.number ? r.number + "次" : "不限次"} · 已用${r.unlock_num}次 · ${r.expired ? "已过期" : "有效"}</div>
              </div>
              <ha-button size="small" data-aid="${r.authorized_id}">删除</ha-button>
            </div>
          `
        )
        .join("");
      listEl.querySelectorAll("ha-button[data-aid]").forEach((btn) => {
        btn.addEventListener("click", () => this._revoke(Number(btn.getAttribute("data-aid"))));
      });
    } catch (e) {
      const currentEl = this.shadowRoot.querySelector("#ov-tp-list");
      if (currentEl) {
        currentEl.innerHTML = `<div class='error'>${this._escapeHtml(
          e.message || String(e)
        )}</div>`;
      }
    }
  }

  async _revoke(authorizedId) {
    try {
      await this._hass.callWS({
        type: "call_service",
        domain: "orvibohomebridge",
        service: "revoke_temp_password",
        service_data: {
          device_id: this._deviceId,
          authorized_id: authorizedId,
        },
      });
    } catch (e) {
      console.error("ORVIBO card: 删除失败", e);
    }
    this._lastListAt = 0;
    this._listLoaded = false;
    this._loadList();
  }

  _escapeHtml(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}

if (!customElements.get("orvibo-door-lock-card")) {
  customElements.define("orvibo-door-lock-card", OrviboDoorLockCard);
} else {
  console.warn("orvibo-door-lock-card 已定义，跳过重复注册");
}
window.customCards = window.customCards || [];
window.customCards.push({
  type: "orvibo-door-lock-card",
  name: "ORVIBO 门锁",
  description: "门锁状态 + 临时密码下发与管理",
  preview: false,
});
