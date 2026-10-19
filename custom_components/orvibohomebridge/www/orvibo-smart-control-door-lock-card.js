/**
 * ORVIBO 门锁卡片（纯原生 JS，无第三方依赖）。
 *
 * 功能：
 *  - 门锁状态总览（锁状态/门磁/电池/最近截图）
 *  - 下发临时密码（type/minutes/number/phone/name，可选短信）
 *  - 临时密码列表管理（查看/删除/过期状态）
 *
 * 用法（Lovelace 卡片）：
 *   type: custom:orvibo-smart-control-door-lock-card
 *   device_id: "<your-door-lock-device-id>"   # 可选，留空自动选第一把门锁
 *   history_limit: 30                         # 可选，卡片显示的历史截图数量
 *
 * 仅显示临时密码管理：
 *   type: custom:orvibo-smart-control-temp-password-card
 *   device_id: "<your-door-lock-device-id>"
 */

const ORVIBO_PREFIX = "orvibo_smart_control_";

class OrviboSmartControlDoorLockCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._entities = {};   // device_id -> {kind -> entity_id}
    this._deviceName = "";
    this._tempResult = "";
    this._tempError = "";
    this._notice = "";
    this._noticeKind = "";
    this._granting = false;
    this._revokingId = null;
    this._listLoading = false;
    this._listError = "";
    this._records = null;
    this._draft = {
      type: "2",
      minutes: "1440",
      number: "1",
      phone: "",
      name: "",
    };
    this._entitiesLoaded = false;
    this._listOpen = false;
    this._grantOpen = false;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._events = [];
    this._eventsLoaded = false;
    this._eventsLoading = false;
    this._eventsError = "";
    this._eventsRequest = null;
    this._historyLimit = 30;
    this._eventsLastCam = "";
    this._lastReloadAt = 0;
    this._lightboxEvent = null;
    this._lightboxOpener = "";
    this._entitiesLoading = false;
    this.attachShadow({ mode: "open" });
  }

  static LIST_THROTTLE_MS = 60000;

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._deviceId = "";
    this._entities = {};
    this._entitiesLoaded = false;
    this._records = null;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._events = [];
    this._eventsLoaded = false;
    this._eventsLoading = false;
    this._eventsError = "";
    this._eventsRequest = null;
    this._eventsLastCam = "";
    this._lightboxEvent = null;
    const requestedHistoryLimit = Number(this._config.history_limit);
    this._historyLimit = Number.isInteger(requestedHistoryLimit)
      ? Math.min(500, Math.max(1, requestedHistoryLimit))
      : 30;
    this._loadEntities();
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    if (!this._entitiesLoaded) {
      this._loadEntities();
    } else {
      if (
        (!this._entities.door || !this._entities.lock_state) &&
        Date.now() - this._lastReloadAt > 60000
      ) {
        this._lastReloadAt = Date.now();
        this._entitiesLoaded = false;
        this._loadEntities();
      }
      if (!previous || Object.values(this._entities).some(
        (id) => previous.states[id] !== hass.states[id]
      )) this._render();
    }
  }

  getCardSize() {
    return this._config.temp_password_only ? 5 : 8;
  }

  async _loadEntities() {
    if (!this._hass || this._entitiesLoading) return;
    this._entitiesLoading = true;
    try {
      const entities = await this._hass.callWS({ type: "config/entity_registry/list" });
      const devices = await this._hass.callWS({ type: "config/device_registry/list" });
      const lockDevices = new Map();
      for (const dev of devices) {
        const identifier = (dev.identifiers || []).find(
          (value) =>
            Array.isArray(value) &&
            value[0] === "orvibo_smart_control" &&
            value[1]
        );
        if (!identifier) continue;
        lockDevices.set(dev.id, {
          name: dev.name_by_user || dev.name || "门锁",
          deviceId: String(identifier[1]),
        });
      }
      const byDevice = {};
      for (const ent of entities) {
        if (!ent.unique_id || !ent.unique_id.startsWith(ORVIBO_PREFIX)) continue;
        if (!ent.device_id) continue;
        const dev = lockDevices.get(ent.device_id);
        if (!dev) continue;
        const uid = ent.unique_id;
        let kind = "unknown";
        // door_lock_doorbell 含 door_lock_door 子串，必须优先匹配
        if (uid.includes("door_lock_doorbell")) kind = "doorbell";
        else if (uid.includes("door_lock_state")) kind = "lock_state";
        else if (uid.includes("door_lock_door")) kind = "door";
        else if (uid.includes("dry_battery")) kind = "dry_battery";
        else if (uid.includes("lithium_battery")) kind = "lithium_battery";
        else if (uid.includes("door_lock_unlock")) kind = "unlock";
        else if (uid.includes("temp_password")) kind = "temp_password";
        else if (uid.includes("camera")) kind = "camera";
        byDevice[dev.deviceId] = byDevice[dev.deviceId] || { name: dev.name, entities: {} };
        byDevice[dev.deviceId].entities[kind] = ent.entity_id;
      }
      for (const [deviceId, item] of Object.entries(byDevice)) {
        if (
          !item.entities.lock_state &&
          !item.entities.door &&
          !item.entities.unlock
        ) {
          delete byDevice[deviceId];
        }
      }
      const requested = this._config.device_id;
      const candidates = Object.keys(byDevice);
      let deviceId = requested && byDevice[requested] ? requested : "";
      if (!deviceId) {
        // 多设备匹配时选择实体最全的门锁（锁状态+门磁+截图都在的优先）
        let bestScore = -1;
        for (const id of candidates) {
          const ents = byDevice[id].entities;
          const score =
            (ents.lock_state ? 2 : 0) +
            (ents.door ? 2 : 0) +
            (ents.camera ? 1 : 0) +
            Object.keys(ents).length;
          if (score > bestScore) {
            bestScore = score;
            deviceId = id;
          }
        }
      }
      if (deviceId && byDevice[deviceId]) {
        this._deviceId = deviceId;
        this._deviceName = byDevice[deviceId].name;
        this._entities = byDevice[deviceId].entities;
        this._entitiesLoaded = true;
      }
      this._entitiesLoaded = true;
    } catch (e) {
      console.error("ORVIBO card: 加载实体失败", e);
    }
    this._entitiesLoading = false;
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

  _lockState() {
    const entityId = this._entities.lock_state;
    const state = this._state(entityId);
    if (
      state &&
      state !== "unknown" &&
      state !== "unavailable"
    ) {
      return state;
    }
    const doorOpen = this._attr(entityId, "door_open");
    const insideLocked = this._attr(entityId, "inside_locked");
    const locked = this._attr(entityId, "locked");
    if (doorOpen === true && locked === true) return "abnormal";
    if (doorOpen === true) return "unlocked";
    if (insideLocked === true) return "inside_locked";
    if (locked === true) return "locked";
    if (locked === false) return "unlocked";
    return "unknown";
  }

  _batterySummary() {
    const batteries = [
      {
        label: "锂电池",
        state: this._state(this._entities.lithium_battery),
        installed: this._attr(this._entities.lithium_battery, "is_setup"),
      },
      {
        label: "干电池",
        state: this._state(this._entities.dry_battery),
        installed: this._attr(this._entities.dry_battery, "is_setup"),
      },
    ];
    const hasLevel = ({ state }) =>
      ["string", "number"].includes(typeof state) &&
      state != null &&
      state !== "" &&
      state !== "unknown" &&
      state !== "unavailable" &&
      String(state).trim() !== "" &&
      Number.isInteger(Number(state)) && Number(state) >= 0 && Number(state) <= 100;
    const battery =
      batteries.find((item) => item.installed !== false && hasLevel(item)) ||
      batteries.find((item) => item.installed === true);
    if (!battery) return { label: "电池", value: "未知", level: null };
    return {
      label: battery.label,
      value: hasLevel(battery) ? `${Number(battery.state)}%` : "未知",
      level: hasLevel(battery) ? Number(battery.state) : null,
    };
  }

  _doorSummary() {
    const state = this._state(this._entities.door);
    const open = state === "on" ? true : state === "off" ? false
      : this._attr(this._entities.lock_state, "door_open");
    return open === true ? { label: "门已开", icon: "mdi:door-open", tone: "warning" }
      : open === false ? { label: "门已关", icon: "mdi:door-closed", tone: "" }
        : { label: "门磁未知", icon: "mdi:door", tone: "muted" };
  }

  _openingSummary() {
    const entity = this._entities.unlock;
    const state = this._state(entity);
    const direction = this._attr(entity, "open_direction");
    const name = this._attr(entity, "unlock_user_name");
    const method = this._attr(entity, "unlock_method");
    const methods = { fingerprint: "指纹", password: "密码", temporary_password: "临时密码",
      card: "卡片", key: "钥匙", face: "人脸" };
    const hasEvent = state && !["无", "unknown", "unavailable"].includes(state);
    return {
      label: !hasEvent ? "暂无开门记录" : direction === "inside" ? "人员未知" : name || state,
      direction: hasEvent ? ({ inside: "内开", outside: "外开" }[direction] || "") : "",
      detail: hasEvent ? [direction === "inside" ? null : methods[method], this._shortTime(this._attr(entity, "unlock_time"))]
        .filter(Boolean).join(" · ") : "开门后在这里查看",
    };
  }

  _shortTime(ts) {
    if (!ts || !Number.isFinite(Number(ts))) return "";
    const date = new Date(Number(ts) * 1000);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date();
    const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    const day = date.toDateString() === today.toDateString() ? "今天"
      : date.toDateString() === yesterday.toDateString() ? "昨天"
        : `${date.getFullYear() === today.getFullYear() ? "" : `${date.getFullYear()}/`}${date.getMonth() + 1}/${date.getDate()}`;
    return `${day} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }

  _fmtTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  _recordsMarkup() {
    if (this._listLoading && this._records === null) {
      return "<div class='empty'>正在加载授权...</div>";
    }
    if (this._listError) {
      return `<div class="message error">${this._escapeHtml(this._listError)}</div>`;
    }
    if (!Array.isArray(this._records) || !this._records.length) {
      return "<div class='empty'>暂无临时密码授权</div>";
    }
    return this._records
      .map((record) => {
        const authorizedId = Number(record.authorized_id);
        const name = record.name || "临时用户";
        const validity = record.end_time
          ? `有效至 ${this._fmtTs(record.end_time)}`
          : record.start_time
            ? `生效于 ${this._fmtTs(record.start_time)}`
            : "长期有效";
        const usage = Number(record.number)
          ? `已用 ${Number(record.unlock_num) || 0} / ${Number(record.number)} 次`
          : "不限次数";
        return `
          <div class="tp-item ${record.expired ? "expired" : ""}">
            <div class="tp-content">
              <div class="tp-heading">
                <span>${this._escapeHtml(name)}</span>
                <span class="status ${record.expired ? "expired" : "active"}">${record.expired ? "已过期" : "有效"}</span>
              </div>
              <div class="meta">授权 #${this._escapeHtml(authorizedId)} · ${this._escapeHtml(validity)}</div>
              <div class="meta">${this._escapeHtml(usage)}</div>
            </div>
            <button class="icon-button danger" data-aid="${this._escapeHtml(authorizedId)}" title="撤销授权" aria-label="撤销授权" ${this._revokingId === authorizedId ? "disabled" : ""}>
              <ha-icon icon="mdi:delete-outline"></ha-icon>
            </button>
          </div>`;
      })
      .join("");
  }

  _render() {
    const root = this.shadowRoot;
    if (!this._hass || !this._deviceId) {
      root.innerHTML = `<ha-card style="padding:20px">${!this._hass ? "正在连接门锁…" : "未找到门锁，请确认集成已加载或配置 device_id"}</ha-card>`;
      return;
    }
    const active = root.activeElement;
    const focusId = active && active.id;
    const selection = active && typeof active.selectionStart === "number"
      ? [active.selectionStart, active.selectionEnd] : null;
    const scrollLeft = root.querySelector(".events")?.scrollLeft || 0;
    const e = this._entities;
    const lockState = this._lockState();
    const lock = {
      locked: { label: "已上锁", icon: "mdi:lock", tone: "secure" },
      inside_locked: { label: "已反锁", icon: "mdi:shield-lock", tone: "secure" },
      unlocked: { label: "未上锁", icon: "mdi:lock-open-variant", tone: "warning" },
      abnormal: { label: "锁状态异常", icon: "mdi:alert-circle-outline", tone: "danger" },
      unknown: { label: "锁状态未知", icon: "mdi:lock-question", tone: "muted" },
    }[lockState] || { label: "锁状态未知", icon: "mdi:lock-question", tone: "muted" };
    const door = this._doorSummary();
    const insideLocked = this._attr(e.lock_state, "inside_locked");
    const reverseLabel = insideLocked === true ? "已反锁" : insideLocked === false ? "未反锁" : "反锁未知";
    const battery = this._batterySummary();
    const opening = this._openingSummary();
    const tempOnly = this._config.temp_password_only === true;
    const camera = e.camera ? this._hass.states[e.camera] : null;
    const camKey = camera ? `${camera.state}|${camera.last_updated}` : "";
    if (!tempOnly && (!this._eventsLoaded || (camKey && camKey !== this._eventsLastCam))) this._loadEvents();
    this._eventsLastCam = camKey;

    root.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <header class="header">
          <div class="heading"><div class="eyebrow">${tempOnly ? this._escapeHtml(this._deviceName) : "门锁总览"}</div>
            <h2 class="title">${this._escapeHtml(tempOnly ? "临时密码管理" : this._deviceName)}</h2></div>
          ${tempOnly ? `<span class="header-icon"><ha-icon icon="mdi:key-chain"></ha-icon></span>` : `
            <div class="battery ${battery.level === null ? "unknown" : battery.level <= 20 ? "low" : ""}" aria-label="${this._escapeHtml(`${battery.label} ${battery.value}`)}">
              <ha-icon icon="${battery.level === null ? "mdi:battery-unknown" : battery.level <= 20 ? "mdi:battery-alert-variant-outline" : "mdi:battery-high"}"></ha-icon>
              <span><small>${this._escapeHtml(battery.label)}</small><strong>${this._escapeHtml(battery.value)}</strong></span>
            </div>`}
        </header>
        ${tempOnly ? "" : `
          <section class="lock-overview ${lock.tone}" aria-label="当前门锁状态">
            <div class="lock-symbol"><ha-icon icon="${lock.icon}"></ha-icon></div>
            <div class="lock-content"><div class="lock-state">${lock.label}</div>
              <div class="lock-details"><span class="${door.tone}"><ha-icon icon="${door.icon}"></ha-icon>${door.label}</span>
                <span><ha-icon icon="mdi:shield-lock-outline"></ha-icon>${reverseLabel}</span></div>
            </div>
          </section>
          <section class="opening" aria-label="最近开门">
            <span class="opening-icon"><ha-icon icon="mdi:account-outline"></ha-icon></span>
            <div class="opening-content"><div class="eyebrow">最近开门</div>
              <div class="opening-name">${this._escapeHtml(opening.label)}</div>
              ${opening.detail ? `<div class="meta">${this._escapeHtml(opening.detail)}</div>` : ""}</div>
            ${opening.direction ? `<span class="direction-tag">${opening.direction}</span>` : ""}
          </section>
          <section class="section history-section">
            <div class="section-title"><div><span class="section-label"><ha-icon icon="mdi:camera-outline"></ha-icon>猫眼抓拍</span>
              <div class="meta history-meta">${this._events.length ? `最近 ${Math.min(this._events.length, this._historyLimit)} 张 · 点击查看大图` : "开门与来访记录"}</div></div>
              <button type="button" class="icon-button ${this._eventsLoading ? "loading" : ""}" id="ov-events-refresh" title="刷新抓拍" aria-label="刷新抓拍" ${this._eventsLoading ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon></button>
            </div>
            ${this._eventsMarkup()}
          </section>`}
        <section class="section ${tempOnly ? "first" : ""}">
          <button type="button" class="section-toggle" id="ov-grant-toggle" aria-expanded="${this._grantOpen}" aria-controls="ov-grant-form">
            <span class="section-label"><ha-icon icon="mdi:key-plus"></ha-icon>生成临时密码</span>
            <ha-icon icon="mdi:chevron-${this._grantOpen ? "up" : "down"}"></ha-icon>
          </button>
          <div id="ov-grant-form" class="form" ${this._grantOpen ? "" : "hidden"}>
            <label>密码类型<select id="ov-tp-type"><option value="2" ${this._draft.type === "2" ? "selected" : ""}>临时密码</option><option value="1" ${this._draft.type === "1" ? "selected" : ""}>限时密码</option></select></label>
            <label>有效时长（分钟）<input id="ov-tp-minutes" type="number" value="${this._escapeHtml(this._draft.minutes)}" min="1" max="525600" inputmode="numeric" /></label>
            <label>可用次数<input id="ov-tp-number" type="number" value="${this._escapeHtml(this._draft.number)}" min="0" max="100" inputmode="numeric" aria-describedby="ov-usage-help" /></label>
            <div class="meta form-help" id="ov-usage-help">0 表示不限次数</div>
            <label>手机号<input id="ov-tp-phone" type="tel" value="${this._escapeHtml(this._draft.phone)}" placeholder="可选，用于短信通知" autocomplete="tel" /></label>
            <label>访客名称<input id="ov-tp-name" type="text" value="${this._escapeHtml(this._draft.name)}" placeholder="例如：保洁、访客" maxlength="64" autocomplete="off" /></label>
            <button type="button" class="primary-button" id="ov-tp-grant" ${this._granting ? "disabled" : ""}>${this._granting ? "正在生成…" : "生成临时密码"}</button>
            ${this._tempError ? `<div class="message error" role="alert">${this._escapeHtml(this._tempError)}</div>` : ""}
            ${this._tempResult ? `<div class="result" role="status" aria-live="polite"><div><span>本次临时密码</span><strong>${this._escapeHtml(this._tempResult)}</strong><small>仅本次显示，请妥善保存</small></div><button type="button" class="icon-button" id="ov-tp-copy" title="复制密码" aria-label="复制密码"><ha-icon icon="mdi:content-copy"></ha-icon></button></div>` : ""}
          </div>
        </section>
        <section class="section">
          <div class="list-heading"><button type="button" class="section-toggle" id="ov-tp-toggle" aria-expanded="${this._listOpen}" aria-controls="ov-tp-list"><span class="section-label"><ha-icon icon="mdi:key-chain-variant"></ha-icon>授权列表${Array.isArray(this._records) ? `<span class="count">${this._records.length}</span>` : ""}</span><ha-icon icon="mdi:chevron-${this._listOpen ? "up" : "down"}"></ha-icon></button>
            <button type="button" class="icon-button" id="ov-tp-refresh" title="刷新授权" aria-label="刷新授权" ${this._listLoading ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon></button></div>
          <div id="ov-tp-list" class="tp-list" ${this._listOpen ? "" : "hidden"}>${this._listOpen ? this._recordsMarkup() : ""}</div>
        </section>
        ${this._notice ? `<div class="message ${this._noticeKind === "error" ? "error" : "success"}" role="status" aria-live="polite">${this._escapeHtml(this._notice)}</div>` : ""}
      </ha-card>
      ${!tempOnly ? this._lightboxMarkup() : ""}`;

    root.querySelector("#ov-tp-grant").addEventListener("click", () => this._grant());
    for (const [selector, key] of Object.entries({ "#ov-tp-type": "type", "#ov-tp-minutes": "minutes", "#ov-tp-number": "number", "#ov-tp-phone": "phone", "#ov-tp-name": "name" })) {
      root.querySelector(selector).addEventListener("input", (event) => { this._draft[key] = event.target.value; });
    }
    root.querySelector("#ov-grant-toggle").addEventListener("click", () => { this._grantOpen = !this._grantOpen; this._render(); });
    root.querySelector("#ov-tp-toggle").addEventListener("click", () => { this._listOpen = !this._listOpen; this._render(); });
    root.querySelector("#ov-tp-refresh").addEventListener("click", () => {
      this._lastListAt = 0; this._listLoaded = false; this._listOpen = true; this._loadList();
    });
    root.querySelector("#ov-events-refresh")?.addEventListener("click", () => this._loadEvents(true));
    root.querySelectorAll("button[data-aid]").forEach((button) => {
      button.addEventListener("click", () => this._revoke(Number(button.getAttribute("data-aid"))));
    });
    root.querySelector("#ov-tp-copy")?.addEventListener("click", () => this._copyPassword());
    root.querySelectorAll("button[data-idx]").forEach((button) => {
      button.addEventListener("click", () => this._openSnapshot(Number(button.dataset.idx), button.id));
    });
    root.querySelectorAll(".snapshot-image").forEach((image) => {
      image.addEventListener("error", () => { image.hidden = true; image.parentElement.classList.add("image-failed"); });
    });
    const dialog = this._lightboxEvent ? root.querySelector("#ov-lightbox") : null;
    if (dialog) {
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); this._closeLightbox(); });
      dialog.addEventListener("click", (event) => { if (event.target === dialog) this._closeLightbox(); });
      dialog.addEventListener("keydown", (event) => {
        if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); this._moveSnapshot(event.key === "ArrowLeft" ? -1 : 1); }
      });
      root.querySelector("#ov-lightbox-close").addEventListener("click", () => this._closeLightbox());
      root.querySelector("#ov-lightbox-prev").addEventListener("click", () => this._moveSnapshot(-1));
      root.querySelector("#ov-lightbox-next").addEventListener("click", () => this._moveSnapshot(1));
      dialog.showModal?.();
    }
    const rail = root.querySelector(".events");
    if (rail) rail.scrollLeft = scrollLeft;
    const restored = focusId ? root.querySelector(`#${focusId}`) : null;
    if (restored) {
      restored.focus?.({ preventScroll: true });
      if (selection && restored.setSelectionRange) restored.setSelectionRange(...selection);
    } else if (dialog) root.querySelector("#ov-lightbox-close").focus?.({ preventScroll: true });
    if (this._listOpen) this._loadList();
  }

  _styles() {
    return `
      :host { display:block; container-type:inline-size; --ov-surface:var(--secondary-background-color, #f3f5f7); --ov-success:var(--success-color, #23845b); --ov-warning:var(--warning-color, #a66b00); --ov-danger:var(--error-color, #db4437); }
      * { box-sizing:border-box; }
      [hidden] { display:none !important; }
      ha-card { display:block; padding:20px; color:var(--primary-text-color); font-size:14px; line-height:1.5; }
      button, input, select { font:inherit; }
      button { color:inherit; cursor:pointer; -webkit-tap-highlight-color:transparent; }
      button:disabled { cursor:default; opacity:.45; }
      button:focus-visible, input:focus-visible, select:focus-visible { outline:2px solid var(--primary-color, #03a9f4); outline-offset:3px; }
      button { transition:background-color .18s, border-color .18s; }
      ha-icon { --mdc-icon-size:20px; flex-shrink:0; }
      .header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:18px; }
      .heading, .opening-content, .lock-content { min-width:0; }
      .eyebrow { font-size:12px; color:var(--secondary-text-color); }
      .title { margin:2px 0 0; font-size:20px; font-weight:650; line-height:1.35; overflow-wrap:anywhere; }
      .battery { display:flex; align-items:center; gap:6px; flex-shrink:0; padding:6px 10px; border:1px solid var(--divider-color); border-radius:10px; }
      .battery ha-icon { color:var(--ov-success); --mdc-icon-size:24px; }
      .battery small { display:block; color:var(--secondary-text-color); font-size:10px; line-height:1.2; }
      .battery strong { font-size:16px; font-variant-numeric:tabular-nums; }
      .battery.low ha-icon { color:var(--ov-danger); }
      .battery.unknown ha-icon { color:var(--secondary-text-color); }
      .header-icon, .opening-icon { display:grid; place-items:center; width:40px; height:40px; border-radius:12px; background:var(--ov-surface); color:var(--secondary-text-color); flex-shrink:0; }
      .lock-overview { --ov-state:var(--secondary-text-color); display:flex; align-items:center; gap:16px; padding:18px; border:1px solid color-mix(in srgb, var(--ov-state) 20%, var(--divider-color)); border-radius:14px; background:color-mix(in srgb, var(--ov-state) 7%, var(--card-background-color, #fff)); }
      .lock-overview.secure { --ov-state:var(--ov-success); }
      .lock-overview.warning { --ov-state:var(--ov-warning); }
      .lock-overview.danger { --ov-state:var(--ov-danger); }
      .lock-symbol { color:var(--ov-state); display:grid; place-items:center; width:48px; height:56px; flex-shrink:0; }
      .lock-symbol ha-icon { --mdc-icon-size:42px; }
      .lock-state { font-size:26px; font-weight:650; line-height:1.2; overflow-wrap:anywhere; }
      .lock-details { display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:8px; color:var(--secondary-text-color); font-size:12px; }
      .lock-details span { display:inline-flex; align-items:center; gap:4px; }
      .lock-details ha-icon { --mdc-icon-size:15px; }
      .lock-details .warning { color:var(--primary-text-color); font-weight:600; }
      .opening { display:flex; gap:12px; align-items:center; padding:18px 0 2px; }
      .opening-content { flex:1; }
      .opening-name { font-size:16px; font-weight:600; overflow-wrap:anywhere; }
      .direction-tag, .count { border-radius:6px; background:var(--ov-surface); padding:3px 9px; font-size:12px; white-space:nowrap; }
      .section { border-top:1px solid var(--divider-color); margin-top:16px; padding-top:10px; }
      .section.first { border-top:0; margin-top:0; padding-top:0; }
      .section-title { display:flex; justify-content:space-between; align-items:center; gap:8px; min-height:44px; margin-bottom:10px; }
      .section-label { display:inline-flex; align-items:center; gap:8px; font-weight:600; }
      .section-label ha-icon { color:var(--secondary-text-color); }
      .history-meta { margin:2px 0 0 28px; }
      .section-toggle { display:flex; justify-content:space-between; align-items:center; gap:12px; width:100%; min-height:44px; padding:0; border:0; background:transparent; text-align:left; }
      .section-toggle:hover { color:var(--primary-color); }
      .list-heading { display:flex; align-items:center; gap:8px; }
      .section-toggle > ha-icon { color:var(--secondary-text-color); }
      .count { font-size:11px; padding:0 6px; font-weight:400; }
      .icon-button { display:inline-flex; align-items:center; justify-content:center; width:44px; height:44px; flex:0 0 44px; padding:0; border:0; border-radius:10px; background:transparent; }
      .icon-button:hover { background:var(--ov-surface); }
      .icon-button.danger { color:var(--ov-danger); }
      .icon-button.loading ha-icon { animation:ov-spin 1.4s linear infinite; }
      .snapshot { position:relative; display:block; width:100%; aspect-ratio:16/9; max-height:240px; padding:0; overflow:hidden; border:1px solid var(--divider-color); border-radius:12px; background:var(--ov-surface); }
      .snapshot img { display:block; width:100%; height:100%; object-fit:contain; background:#101820; }
      .snapshot-caption { position:absolute; inset:auto 0 0; display:flex; justify-content:space-between; gap:8px; padding:24px 12px 10px; color:#fff; background:linear-gradient(transparent, rgba(0,0,0,.78)); font-size:12px; }
      .snapshot-caption strong { font-weight:500; }
      .events { display:flex; gap:8px; overflow-x:auto; padding:10px 2px 5px; scroll-snap-type:x proximity; scrollbar-width:thin; }
      .ev-item { flex:0 0 88px; min-width:0; border:0; padding:0; border-radius:8px; background:transparent; text-align:left; scroll-snap-align:start; }
      .thumb { position:relative; display:block; height:60px; border-radius:8px; overflow:hidden; background:var(--ov-surface); border:1px solid var(--divider-color); }
      .thumb img { display:block; width:100%; height:100%; object-fit:cover; }
      .ev-item:hover .thumb { border-color:var(--primary-color); }
      .ev-time { display:block; margin-top:4px; font-size:11px; color:var(--secondary-text-color); font-variant-numeric:tabular-nums; white-space:nowrap; }
      .image-fallback { display:none; position:absolute; inset:0; align-items:center; justify-content:center; flex-direction:column; gap:6px; color:var(--secondary-text-color); font-size:12px; }
      .image-failed .image-fallback { display:flex; }
      .image-failed .snapshot-caption { display:none; }
      .snapshot-empty { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; min-height:154px; border:1px dashed var(--divider-color); border-radius:12px; background:var(--ov-surface); color:var(--secondary-text-color); text-align:center; padding:20px; }
      .snapshot-empty ha-icon { --mdc-icon-size:30px; margin-bottom:6px; }
      .snapshot-empty strong { font-size:14px; font-weight:500; color:var(--primary-text-color); }
      .snapshot-empty span { font-size:12px; }
      .form { display:grid; gap:12px; margin-top:12px; }
      .form label { display:grid; grid-template-columns:116px minmax(0,1fr); align-items:center; gap:10px; color:var(--secondary-text-color); font-size:13px; }
      .form input, .form select { min-width:0; min-height:44px; width:100%; border:1px solid var(--divider-color); border-radius:8px; padding:9px 11px; color:var(--primary-text-color); background:var(--card-background-color); font-size:16px; }
      .form-help { margin-top:-8px; padding-left:126px; }
      .primary-button { display:block; min-height:44px; border:1px solid color-mix(in srgb, var(--primary-color, #03a9f4) 40%, var(--divider-color)); border-radius:9px; background:color-mix(in srgb, var(--primary-color, #03a9f4) 14%, var(--card-background-color, #fff)); color:var(--primary-text-color); padding:10px 16px; font-weight:600; }
      .primary-button:hover { filter:brightness(.95); }
      .result { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px; border-radius:10px; background:var(--ov-surface); border:1px solid var(--divider-color); }
      .result span, .result small { display:block; color:var(--secondary-text-color); font-size:12px; }
      .result strong { display:block; margin:3px 0; font-size:28px; font-variant-numeric:tabular-nums; letter-spacing:2px; }
      .tp-list { display:grid; gap:8px; margin-top:10px; }
      .tp-item { display:flex; justify-content:space-between; align-items:center; gap:12px; border:1px solid var(--divider-color); border-radius:10px; padding:10px 12px; }
      .tp-content { min-width:0; }
      .tp-heading { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-weight:600; overflow-wrap:anywhere; }
      .status { border-radius:5px; padding:2px 6px; font-size:11px; font-weight:400; }
      .status.active { background:color-mix(in srgb, var(--ov-success) 12%, var(--card-background-color)); }
      .status.expired { background:var(--ov-surface); color:var(--secondary-text-color); }
      .meta { color:var(--secondary-text-color); font-size:12px; line-height:1.5; overflow-wrap:anywhere; }
      .empty { padding:18px 0; color:var(--secondary-text-color); text-align:center; }
      .message { margin:8px 0; padding:10px 12px; border-radius:8px; font-size:13px; overflow-wrap:anywhere; }
      .message.success { background:color-mix(in srgb, var(--ov-success) 9%, var(--card-background-color)); }
      .message.error { background:color-mix(in srgb, var(--ov-danger) 9%, var(--card-background-color)); color:var(--primary-text-color); border-left:3px solid var(--ov-danger); }
      .lightbox { width:min(1000px,96vw); max-width:96vw; max-height:94dvh; padding:16px; border:0; border-radius:16px; background:#151a20; color:#f5f7fa; }
      .lightbox::backdrop { background:rgba(0,0,0,.86); }
      .lightbox-toolbar, .lightbox-footer { display:flex; align-items:center; justify-content:space-between; gap:12px; }
      .lightbox-toolbar { margin-bottom:12px; }
      .lightbox-toolbar strong { display:block; font-size:15px; }
      .lightbox-toolbar small { color:#bec7d0; }
      .lightbox .icon-button:hover { background:#303841; }
      .lightbox-image { position:relative; min-height:140px; display:grid; place-items:center; }
      .lightbox-image img { display:block; max-width:100%; max-height:calc(94dvh - 180px); object-fit:contain; border-radius:8px; }
      .lightbox .image-fallback { color:#bec7d0; }
      .lightbox-footer { margin-top:12px; font-size:12px; color:#bec7d0; }
      @keyframes ov-spin { to { transform:rotate(360deg); } }
      @container (max-width:360px) {
        ha-card { padding:16px; }
        .lock-overview { padding:16px 12px; gap:12px; }
        .lock-state { font-size:24px; }
        .lock-details { gap:4px 10px; }
        .form label { grid-template-columns:1fr; gap:5px; }
        .form-help { padding-left:0; }
      }
      @media (prefers-reduced-motion:reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
    `;
  }


  async _grant() {
    if (this._granting) return;
    this._tempError = "";
    this._tempResult = "";
    this._notice = "";
    const minutes = Number(this._draft.minutes);
    const number = Number(this._draft.number);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 525600) {
      this._tempError = "时长必须是 1 到 525600 之间的整数";
      this._render();
      return;
    }
    if (!Number.isInteger(number) || number < 0 || number > 100) {
      this._tempError = "次数必须是 0 到 100 之间的整数";
      this._render();
      return;
    }
    const data = {
      device_id: this._deviceId,
      type: Number(this._draft.type),
      minutes,
      number,
      phone: this._draft.phone.trim(),
      name: this._draft.name.trim(),
    };
    this._granting = true;
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "grant_temp_password",
        service_data: data,
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) {
        this._tempError = res.error;
      } else if (res && res.password) {
        this._tempResult = String(res.password);
        this._listOpen = true;
      } else {
        this._tempError = "门锁未返回临时密码，请稍后重试";
      }
    } catch (e) {
      this._tempError = e.message || String(e);
    }
    this._granting = false;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._records = null;
    this._render();
    if (this._listOpen) {
      this._loadList();
    }
  }

  async _loadList() {
    if (!this._hass || !this._deviceId || this._listLoading) return;
    const now = Date.now();
    if (
      this._listLoaded &&
      now - this._lastListAt < OrviboSmartControlDoorLockCard.LIST_THROTTLE_MS
    ) {
      return; // 节流：60 秒内不重复拉取
    }
    this._listLoading = true;
    this._listError = "";
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "list_temp_passwords",
        service_data: { device_id: this._deviceId },
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) throw new Error(res.error);
      const records = (res && res[this._deviceId]) || [];
      if (!Array.isArray(records)) throw new Error("授权列表响应格式无效");
      this._records = records;
      this._listLoaded = true;
      this._lastListAt = Date.now();
    } catch (e) {
      this._listError = e.message || String(e);
      this._records = null;
    } finally {
      this._listLoading = false;
      this._render();
    }
  }

  async _revoke(authorizedId) {
    if (!Number.isInteger(authorizedId) || authorizedId <= 0 || this._revokingId) return;
    const record = Array.isArray(this._records)
      ? this._records.find((item) => Number(item.authorized_id) === authorizedId)
      : null;
    const label = record && record.name ? `“${record.name}”` : `#${authorizedId}`;
    if (!window.confirm(`确定撤销临时密码授权 ${label} 吗？撤销后将立即失效。`)) return;
    this._revokingId = authorizedId;
    this._notice = "";
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "revoke_temp_password",
        service_data: {
          device_id: this._deviceId,
          authorized_id: authorizedId,
        },
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) throw new Error(res.error);
      this._records = Array.isArray(this._records)
        ? this._records.filter((item) => Number(item.authorized_id) !== authorizedId)
        : this._records;
      this._notice = "临时密码授权已撤销";
      this._noticeKind = "success";
    } catch (e) {
      this._notice = e.message || String(e);
      this._noticeKind = "error";
    }
    this._revokingId = null;
    this._lastListAt = Date.now();
    this._listLoaded = true;
    this._render();
  }

  async _copyPassword() {
    if (!this._tempResult) return;
    try {
      await navigator.clipboard.writeText(this._tempResult);
      this._notice = "临时密码已复制";
      this._noticeKind = "success";
    } catch (e) {
      this._notice = "无法复制，请手动记录临时密码";
      this._noticeKind = "error";
    }
    this._render();
  }

  _escapeHtml(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _eventLabel(kind) {
    const labels = {
      ring: "有人来访",
      visit: "有人来访",
      loiter: "逗留",
      picklock: "异常开门",
      message: "消息",
    };
    return labels[kind] || kind || "事件";
  }

  _eventsMarkup() {
    const error = this._eventsError
      ? `<div class="message error" role="status">${this._escapeHtml(this._eventsError)}</div>` : "";
    if (!this._events.length) {
      return `${error}<div class="snapshot-empty" role="status"><ha-icon icon="mdi:camera-outline"></ha-icon>
        <strong>${this._eventsLoading ? "正在加载抓拍…" : this._eventsError ? "暂时无法获取抓拍" : "还没有抓拍记录"}</strong>
        <span>${this._eventsError ? "点击右上角刷新重试" : "门锁上报抓拍后会显示在这里"}</span></div>`;
    }
    const latest = this._events[0];
    const fallback = `<span class="image-fallback"><ha-icon icon="mdi:image-off-outline"></ha-icon><span>图片暂不可用，请刷新</span></span>`;
    return `${error}<button type="button" class="snapshot" id="ov-snapshot-latest" data-idx="0" aria-label="查看最新抓拍：${this._escapeHtml(this._fmtTs(latest.time))}">
      <img class="snapshot-image" src="${this._escapeHtml(latest.url)}" alt="${this._escapeHtml(this._eventLabel(latest.kind))}" />${fallback}
      <span class="snapshot-caption"><strong>最新抓拍 · ${this._escapeHtml(this._eventLabel(latest.kind))}</strong><span>${this._escapeHtml(this._shortTime(latest.time))}</span></span>
    </button>${this._events.length > 1 ? `<div class="events" aria-label="历史抓拍">${this._events.slice(0, this._historyLimit).map((ev, idx) => `
      <button type="button" class="ev-item" id="ov-snapshot-${idx}" data-idx="${idx}" title="${this._escapeHtml(`${this._eventLabel(ev.kind)} · ${this._fmtTs(ev.time)}`)}" aria-label="查看第 ${idx + 1} 张抓拍：${this._escapeHtml(this._fmtTs(ev.time))}">
        <span class="thumb"><img class="snapshot-image" src="${this._escapeHtml(ev.url)}" alt="${this._escapeHtml(this._eventLabel(ev.kind))}" loading="lazy" />${fallback}</span>
        <span class="ev-time">${this._escapeHtml(this._shortTime(ev.time) || "时间未知")}</span>
      </button>`).join("")}</div>` : ""}`;
  }

  _lightboxMarkup() {
    if (!this._lightboxEvent) return "";
    const selected = this._lightboxEvent;
    const index = this._events.findIndex((event) => (event.mediaId || event.url) === (selected.mediaId || selected.url));
    const event = index >= 0 ? this._events[index] : selected;
    return `<dialog class="lightbox" id="ov-lightbox" aria-labelledby="ov-lightbox-title">
      <div class="lightbox-toolbar"><div><strong id="ov-lightbox-title">${this._escapeHtml(this._eventLabel(event.kind))}</strong><small>${this._escapeHtml(this._fmtTs(event.time))}</small></div>
        <button type="button" class="icon-button" id="ov-lightbox-close" aria-label="关闭抓拍预览" autofocus><ha-icon icon="mdi:close"></ha-icon></button></div>
      <div class="lightbox-image"><img class="snapshot-image" src="${this._escapeHtml(event.url)}" alt="${this._escapeHtml(this._eventLabel(event.kind))}" /><span class="image-fallback"><ha-icon icon="mdi:image-off-outline"></ha-icon>图片暂不可用，请关闭后刷新</span></div>
      <div class="lightbox-footer"><button type="button" class="icon-button" id="ov-lightbox-prev" aria-label="上一张抓拍" ${index <= 0 ? "disabled" : ""}><ha-icon icon="mdi:chevron-left"></ha-icon></button>
        <span>${index >= 0 ? `${index + 1} / ${this._events.length}` : "抓拍预览"}</span>
        <button type="button" class="icon-button" id="ov-lightbox-next" aria-label="下一张抓拍" ${index < 0 || index >= this._events.length - 1 ? "disabled" : ""}><ha-icon icon="mdi:chevron-right"></ha-icon></button></div>
    </dialog>`;
  }

  _openSnapshot(index, opener) {
    if (!Number.isInteger(index) || !this._events[index]) return;
    this._lightboxEvent = this._events[index];
    this._lightboxOpener = opener;
    this._render();
  }

  _moveSnapshot(delta) {
    if (!this._lightboxEvent) return;
    const selected = this._lightboxEvent;
    const index = this._events.findIndex((event) => (event.mediaId || event.url) === (selected.mediaId || selected.url));
    if (index < 0 || !this._events[index + delta]) return;
    this._lightboxEvent = this._events[index + delta];
    this._render();
  }

  _closeLightbox() {
    this._lightboxEvent = null;
    this._render();
    this.shadowRoot.querySelector(`#${this._lightboxOpener}`)?.focus?.({ preventScroll: true });
  }

  async _loadEvents(force = false) {
    if (!this._hass || !this._deviceId || this._eventsLoading) return;
    if (force) {
      this._eventsLoaded = false;
    }
    this._eventsLoading = true;
    this._eventsError = "";
    const request = {};
    this._eventsRequest = request;
    const deviceId = this._deviceId;
    const hass = this._hass;
    if (force) this._render();
    try {
      const result = await hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "list_events",
        service_data: { device_id: deviceId, limit: this._historyLimit, media_type: "image" },
        return_response: true,
      });
      const res = result && result.response;
      if (res?.error) throw new Error(res.error);
      if (!Array.isArray(res?.events)) throw new Error("抓拍列表响应无效，请刷新重试");
      const events = res.events.filter((event) => event?.type === "image").slice(0, this._historyLimit);
      const previous = new Map(this._events.map((event) => [event.mediaId, event]));
      const images = new Array(events.length);
      let next = 0;
      let failed = 0;
      // Bound concurrent media resolution; retain server ordering for the gallery.
      await Promise.all(Array.from({ length: Math.min(4, events.length) }, async () => {
        while (next < events.length && this._eventsRequest === request) {
          const index = next++;
          const event = events[index];
          try {
            const resolved = await hass.callWS({ type: "media_source/resolve_media", media_content_id: event.media_id });
            if (!resolved?.url || typeof resolved.url !== "string") throw new Error("图片地址无效");
            images[index] = { kind: event.kind, time: event.time, url: resolved.url, mediaId: event.media_id };
          } catch (error) {
            images[index] = previous.get(event.media_id);
            failed++;
          }
        }
      }));
      if (this._eventsRequest !== request) return;
      const resolvedImages = images.filter(Boolean);
      if (resolvedImages.length || !failed) this._events = resolvedImages;
      this._eventsError = failed ? `${failed} 张抓拍暂时无法加载，请刷新重试` : "";
      this._eventsLoaded = true;
    } catch (e) {
      if (this._eventsRequest !== request) return;
      this._eventsError = e.message || String(e);
      this._eventsLoaded = true;
    }
    if (this._eventsRequest !== request) return;
    this._eventsLoading = false;
    this._render();
  }
}

class OrviboSmartControlTempPasswordCard extends OrviboSmartControlDoorLockCard {
  constructor() {
    super();
    this._grantOpen = true;
    this._listOpen = true;
  }

  setConfig(config) {
    super.setConfig({ ...(config || {}), temp_password_only: true });
  }
}

if (!customElements.get("orvibo-smart-control-door-lock-card")) {
  customElements.define("orvibo-smart-control-door-lock-card", OrviboSmartControlDoorLockCard);
} else {
  console.warn("orvibo-smart-control-door-lock-card 已定义，跳过重复注册");
}
if (!customElements.get("orvibo-smart-control-temp-password-card")) {
  customElements.define(
    "orvibo-smart-control-temp-password-card",
    OrviboSmartControlTempPasswordCard
  );
}
window.customCards = window.customCards || [];
for (const card of [
  {
    type: "orvibo-smart-control-door-lock-card",
    name: "ORVIBO 门锁",
    description: "门锁状态 + 临时密码下发与管理",
    preview: false,
  },
  {
    type: "orvibo-smart-control-temp-password-card",
    name: "ORVIBO 临时密码管理",
    description: "创建、查看和撤销门锁临时密码授权",
    preview: false,
  },
]) {
  if (!window.customCards.some((item) => item.type === card.type)) {
    window.customCards.push(card);
  }
}
