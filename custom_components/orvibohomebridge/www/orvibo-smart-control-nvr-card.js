/* NVR HDD and IPC SD recordings. Calendar values belong to the device. */
(() => {
class OrviboNvrCard extends HTMLElement {
  static getStubConfig() { return {}; }
  setConfig(config) {
    if (config.entity && !String(config.entity).startsWith("camera.")) {
      throw new Error("entity 必须是欧瑞博硬盘或存储卡回放 camera 实体");
    }
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (!this._ui) this._build();
    this._sync();
  }
  set hass(value) {
    this._hass = value;
    if (!this._ui) return;
    this._sync();
    if (this._player) this._player.hass = value;
  }
  getCardSize() { return this._player ? 5 : 3; }
  getGridOptions() { return { columns: 12, rows: "auto", min_columns: 6 }; }
  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card{padding:12px 14px} h2{font-size:16px;font-weight:600;margin:0 0 10px}
        label{display:block;font-size:12px;color:var(--secondary-text-color);margin:0 0 4px}
        select,input,button{font:inherit;font-size:14px;
          color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);
          border-radius:6px;padding:7px 8px;box-sizing:border-box;max-width:100%;min-width:0;min-height:36px}
        select,input{width:100%} button{cursor:pointer} button:disabled{opacity:.5;cursor:default}
        .fields{display:grid;grid-template-columns:minmax(0,1fr) minmax(130px,.8fr);gap:8px}
        .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
        #search{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:var(--primary-color)}
        .note,#status{color:var(--secondary-text-color);line-height:1.45;font-size:12px}
        .note{margin-top:8px} #status{margin:8px 0 0;overflow-wrap:anywhere}
        #status:empty,#days:empty,#player:empty{display:none}
        details{margin-top:10px;border-top:1px solid var(--divider-color);padding-top:8px}
        summary{cursor:pointer;font-size:13px;line-height:24px;user-select:none}
        #clip-count{color:var(--secondary-text-color);font-size:12px}
        #fragments{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;
          max-height:120px;overflow:auto;overscroll-behavior:contain;margin-top:6px}
        #fragments button{width:100%;text-align:center;font-size:12px;white-space:nowrap;min-height:34px;padding:6px}
        #days{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;max-height:140px;overflow:auto}
        #days:empty{display:none} #days button{font-size:12px;padding:4px;min-height:30px}
        .seek{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin-top:6px}
        .advanced-body{padding-top:8px} .advanced-body label{margin-top:10px}
        #player{margin:10px auto 0;max-width:560px;--ha-card-border-width:0}
        #player>*{display:block}
        @media(max-width:340px){.fields{grid-template-columns:minmax(0,1fr)}
          #fragments{grid-template-columns:minmax(0,1fr)} .seek{grid-template-columns:minmax(0,1fr)}}
      </style>
      <ha-card>
        <h2></h2>
        <div class="fields">
          <div><label for="channel">设备 / 通道</label><select id="channel"></select></div>
          <div><label for="day">日期</label><input id="day" type="date"></div>
        </div>
        <div class="row"><button id="search">查询录像</button>
        <button id="stop">停止回放</button></div>
        <div id="status" role="status" aria-live="polite"></div>
        <div id="player"></div>
        <details id="clips" hidden><summary>录像片段 <span id="clip-count"></span></summary>
          <div id="fragments"></div>
        </details>
        <details id="advanced"><summary>更多选项</summary><div class="advanced-body">
          <button id="month">查询当月日期</button><div id="days" class="row"></div>
          <label for="position">指定回放时间</label><div class="seek">
            <input id="position" type="datetime-local" step="1"><button id="play">播放</button>
          </div>
          <div class="note">以设备显示的时间为准。首次连接可能需要约一分钟。</div>
        </div></details>
      </ha-card>`;
    this._ui = (id) => this.shadowRoot.getElementById(id);
    this._ui("day").value = new Date().toLocaleDateString("sv-SE");
    this._ui("channel").addEventListener("change", () => {
      const previous = this._entity;
      this._entity = this._ui("channel").value;
      this._generation = (this._generation || 0) + 1;
      this._clearPlayer();
      this._ui("fragments").replaceChildren();
      this._ui("clips").hidden = true;
      this._ui("days").replaceChildren();
      this._status("");
      if (previous) this._service("stop_hdd_playback", {}, previous).catch(() => {});
    });
    this._ui("day").addEventListener("change", () => {
      this._generation = (this._generation || 0) + 1;
      this._ui("fragments").replaceChildren();
      this._ui("clips").hidden = true;
      this._ui("days").replaceChildren();
      this._status("");
    });
    this._ui("month").addEventListener("click", () => this._search("month"));
    this._ui("search").addEventListener("click", () => this._search("day"));
    this._ui("stop").addEventListener("click", () => this._stop());
    this._ui("play").addEventListener("click", () => {
      const value = this._ui("position").value;
      if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) {
        this._status("请选择有效的录像时间"); return;
      }
      this._play(value.replace(/[-T:]/g, "").padEnd(14, "0"));
    });
  }
  _sync() {
    if (!this._ui || !this._hass) return;
    this.shadowRoot.querySelector("h2").textContent = this._config?.title || "欧瑞博录像回放";
    const states = this._hass.states || {};
    const ids = Object.keys(states).filter((id) => id.startsWith("camera.") &&
      ["hdd_playback", "sd_playback"].includes(states[id].attributes?.video_mode) &&
      (!this._config.entity || id === this._config.entity)).sort();
    const select = this._ui("channel");
    const signature = ids.map((id) => `${id}:${states[id].attributes.friendly_name}`).join("|");
    if (signature !== this._options) {
      this._options = signature;
      select.replaceChildren(...ids.map((id) => {
        const option = document.createElement("option");
        option.value = id; option.textContent = states[id].attributes.friendly_name || id; return option;
      }));
      this._entity = ids.includes(this._entity) ? this._entity : ids[0];
      select.value = this._entity || "";
      if (!ids.length) this._status("未找到录像回放实体，请更新集成、重启 HA 并选择摄像机或录像机设备");
    }
    this._busyButtons();
  }
  _busyButtons() {
    if (!this._ui) return;
    for (const id of ["month", "search", "play", "stop"]) {
      this._ui(id).disabled = !!this._busy || !this._entity;
    }
    this._ui("channel").disabled = !!this._busy;
    this._ui("day").disabled = !!this._busy;
    this._ui("position").disabled = !!this._busy;
    for (const element of this._ui("fragments").querySelectorAll("button")) element.disabled = !!this._busy;
    for (const element of this._ui("days").querySelectorAll("button")) element.disabled = !!this._busy;
  }
  _status(text) { this._ui("status").textContent = text; }
  _clearPlayer() { this._player = null; this._ui("player").replaceChildren(); }
  async _service(service, data, entity = this._entity) {
    const result = await this._hass.callWS({ type: "call_service", domain: "orvibo_smart_control",
      service, service_data: { entity_id: entity, ...data }, return_response: true });
    const response = result?.response || result;
    if (!response || response.error) throw new Error("hdd_service_failed");
    return response;
  }
  async _search(operation) {
    if (this._busy || !this._entity) return;
    const date = this._ui("day").value.replace(/-/g, "");
    if (!/^\d{8}$/.test(date)) { this._status("请选择录像日期"); return; }
    const generation = this._generation;
    this._busy = true; this._busyButtons(); this._status("正在连接设备并查询，请稍候…");
    try {
      const response = await this._service("search_hdd_recordings", { operation,
        date: operation === "month" ? date.slice(0, 6) : date });
      if (generation !== this._generation) return;
      if (response.stage !== "hdd_search_received") {
        this._status(`查询未完成（${this._safeStage(response.stage)}），可稍后重试`); return;
      }
      if (operation === "month") {
        this._ui("days").replaceChildren(...(response.recording_days || []).map((day) => {
          const button = document.createElement("button"); button.textContent = day.slice(6) + " 日";
          button.addEventListener("click", () => {
            this._ui("day").value = `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6)}`;
            this._search("day");
          }); return button;
        }));
        this._status(`当月 ${response.recording_count} 天有录像；点击日期查询片段`);
      } else {
        const fragments = response.fragments || [];
        this._ui("fragments").replaceChildren(...fragments.map((row) => {
          const button = document.createElement("button");
          button.textContent = `${this._time(row.start)} — ${this._time(row.end)}`;
          button.setAttribute("aria-label", `播放 ${this._time(row.start)} 至 ${this._time(row.end)} 的录像`);
          button.addEventListener("click", () => this._play(row.start)); return button;
        }));
        this._ui("clips").hidden = !fragments.length;
        this._ui("clips").open = !!fragments.length && !this._player;
        this._ui("clip-count").textContent = `· ${response.recording_count}${response.fragments_truncated ? "（显示前 256 个）" : ""}`;
        this._status(fragments.length ? "点击片段开始回放" : "当天没有录像片段");
      }
    } catch (_) { this._status("查询失败，请稍后重试；可在开发者工具中查看查询操作响应"); }
    finally { this._busy = false; this._busyButtons(); }
  }
  _time(value) { return `${value.slice(8, 10)}:${value.slice(10, 12)}:${value.slice(12, 14)}`; }
  _safeStage(stage) {
    return ["hdd_query_timeout", "hdd_query_failed", "hdd_query_in_progress"].includes(stage) ? stage : "hdd_query_failed";
  }
  async _play(date) {
    if (this._busy || !this._entity) return;
    const generation = this._generation;
    this._busy = true; this._busyButtons(); this._clearPlayer();
    this._status("正在准备回放，播放器连接可能需要约一分钟…");
    try {
      const response = await this._service("start_hdd_playback", { date });
      if (response.stage !== "playback_selected") throw new Error("hdd_start_failed");
      const helpers = await window.loadCardHelpers();
      if (generation !== this._generation) return;
      this._player = helpers.createCardElement({ type: "picture-entity", entity: this._entity,
        camera_view: "live", aspect_ratio: "16:9", show_name: false, show_state: false });
      this._player.hass = this._hass;
      this._ui("player").replaceChildren(this._player);
      this._ui("clips").open = false;
      this._ui("advanced").open = false;
      this._status(`${date.slice(4, 6)}-${date.slice(6, 8)} ${this._time(date)} · 回放已选择，正在连接`);
    } catch (_) { this._status("回放未能启动，请重新选择片段；也可指定设备时间重试"); }
    finally { this._busy = false; this._busyButtons(); }
  }
  async _stop() {
    if (this._busy || !this._entity) return;
    this._busy = true; this._busyButtons(); this._clearPlayer();
    try { await this._service("stop_hdd_playback", {}); this._status("已停止回放"); }
    catch (_) { this._status("停止失败，请稍后重试"); }
    finally { this._busy = false; this._busyButtons(); }
  }
}
if (!customElements.get("orvibo-smart-control-nvr-card")) {
  customElements.define("orvibo-smart-control-nvr-card", OrviboNvrCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "orvibo-smart-control-nvr-card")) {
  window.customCards.push({ type: "orvibo-smart-control-nvr-card", name: "欧瑞博录像回放",
    description: "按日期查询摄像机存储卡或录像机硬盘片段并回放", preview: false });
}
})();
