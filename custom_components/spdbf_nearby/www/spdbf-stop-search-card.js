// spdbf Public Transport: a dashboard card to find a stop by name (DB, BVG and
// the other configured sources). Focusing the empty field lists the stops
// near you; picking a stop opens its departures (dbf-core.js).
// Loaded on every page by the integration (through spdbf-loader.js), so no
// dashboard resource is needed.
// It appears in the card picker, and getConfigForm gives it Home Assistant's
// own visual editor.
//
//   type: custom:spdbf-stop-search-card
//   placeholder: Search stops          # optional
//   use_device_location: true          # optional; else the tracked person
//   entry_id: ...                      # optional; else the entry following you
(() => {
  if (customElements.get("spdbf-stop-search-card")) return;

  const SOURCE = "spdbf_nearby";
  const DEBOUNCE_MS = 300;
  const MIN_QUERY = 2;
  const LOCATION_TIMEOUT_MS = 5000;
  const NEARBY_MAX_AGE_MS = 60000; // reuse the nearby list for a minute
  // The results float over the dashboard in the browser's top layer, where no
  // card or section can clip them. Without popover support they stay in the
  // card and push the content below down.
  const FLOATING = typeof HTMLElement.prototype.showPopover === "function";

  const FORM_LABELS = {
    placeholder: ["Placeholder", "Text in the empty search field. Default: Search stops"],
    use_device_location: [
      "Use this device's location",
      "Nearby stops around this phone or browser. Off, or without permission: around the person the integration follows.",
    ],
    entry_id: [
      "Integration entry",
      "Only needed with several entries. By default: the one following the logged-in user's person.",
    ],
  };

  const STYLE = `
    :host { display: block; }
    ha-card { overflow: hidden; }
    ha-card.open { border-bottom-left-radius: 0; border-bottom-right-radius: 0; }
    .results[popover] {
      position: fixed; inset: auto; margin: 0; padding: 0; border: none; overflow-y: auto;
      box-sizing: border-box; color: var(--primary-text-color, #212121); font: inherit;
      background: var(--ha-card-background, var(--card-background-color, #fff));
      border-radius: 0 0 var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px);
      box-shadow: 0 8px 16px rgba(0, 0, 0, 0.3);
    }
    .field {
      display: flex; align-items: center; gap: 8px; padding: 0 8px 0 12px; height: 48px;
    }
    .field ha-icon { flex: none; color: var(--secondary-text-color, #727272); }
    input {
      flex: 1; min-width: 0; height: 100%; border: none; outline: none; background: none;
      color: var(--primary-text-color, #212121); font: inherit; font-size: 16px;
    }
    input::placeholder { color: var(--secondary-text-color, #727272); }
    .clear {
      flex: none; width: 36px; height: 36px; border: none; border-radius: 50%; background: none;
      color: var(--secondary-text-color, #727272); font-size: 20px; line-height: 1; cursor: pointer;
    }
    .clear:hover, li:hover, li.active { background: var(--secondary-background-color, rgba(0, 0, 0, 0.06)); }
    .label {
      padding: 8px 16px 4px; border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
      color: var(--secondary-text-color, #727272); font-size: 12px; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.05em;
    }
    ul { list-style: none; margin: 0; padding: 0 0 8px; max-height: 360px; overflow-y: auto; }
    li {
      display: grid; grid-template-columns: 28px 1fr auto; gap: 12px; align-items: center;
      padding: 8px 16px; cursor: pointer;
    }
    li img { width: 28px; height: 28px; }
    .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .meta { text-align: right; white-space: nowrap; color: var(--secondary-text-color, #727272); font-size: 12px; }
    .net {
      display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 4px;
      border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12)); font-size: 11px;
    }
    .empty { padding: 12px 16px; color: var(--secondary-text-color, #727272); font-size: 14px; }
  `;

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));

  class SpdbfStopSearchCard extends HTMLElement {
    constructor() {
      super();
      this._config = {};
      this._open = false;
      this._query = "";
      this._stops = [];
      this._label = "";
      this._message = "";
      this._active = -1;
      this._request = 0;
      this._nearby = null; // { time, stops }
      this._position = null;
      this._timer = null;
      this._onOutside = (ev) => {
        if (!ev.composedPath().includes(this)) this._close();
      };
      this._frame = 0;
      this._placed = "";
    }

    setConfig(config) {
      this._config = { use_device_location: true, ...config };
      this._build();
    }

    set hass(hass) {
      this._hass = hass;
    }

    getCardSize() {
      return 1;
    }

    getGridOptions() {
      return { columns: 12, min_columns: 4, rows: "auto" };
    }

    static getStubConfig() {
      return { use_device_location: true };
    }

    // Rendered by Home Assistant's card editor as a native form.
    static getConfigForm() {
      return {
        schema: [
          { name: "placeholder", selector: { text: {} } },
          { name: "use_device_location", selector: { boolean: {} } },
          { name: "entry_id", selector: { config_entry: { integration: SOURCE } } },
        ],
        computeLabel: (field) => FORM_LABELS[field.name]?.[0],
        computeHelper: (field) => FORM_LABELS[field.name]?.[1],
      };
    }

    connectedCallback() {
      document.addEventListener("click", this._onOutside, true);
    }

    disconnectedCallback() {
      document.removeEventListener("click", this._onOutside, true);
      clearTimeout(this._timer);
      this._close();
    }

    _build() {
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      // A rebuild drops the open list, so start closed.
      this._open = false;
      this._stopFollowing();
      const placeholder = this._config.placeholder ?? "Search stops";
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <ha-card>
          <div class="field">
            <ha-icon icon="mdi:magnify"></ha-icon>
            <input type="text" enterkeyhint="search" autocomplete="off" spellcheck="false"
              placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}"
              role="combobox" aria-expanded="false" aria-controls="results">
            <button class="clear" hidden aria-label="Clear">×</button>
          </div>
          <div class="results" id="results" ${FLOATING ? 'popover="manual"' : "hidden"}></div>
        </ha-card>`;
      const input = this.shadowRoot.querySelector("input");
      input.addEventListener("focus", () => this._show());
      input.addEventListener("click", () => this._show());
      input.addEventListener("input", () => this._onInput(input.value));
      input.addEventListener("keydown", (ev) => this._onKey(ev));
      this.shadowRoot.querySelector(".clear").addEventListener("click", () => {
        input.value = "";
        this._onInput("");
        input.focus();
      });
      this.shadowRoot.querySelector(".results").addEventListener("click", (ev) => {
        const item = ev.target.closest("li[data-index]");
        if (item) this._pick(Number(item.dataset.index));
      });
    }

    _show() {
      if (this._open) return;
      this._open = true;
      if (this._query.length >= MIN_QUERY) this._search(this._query);
      else this._loadNearby();
    }

    _close() {
      if (!this._open) return;
      this._open = false;
      this._request += 1;
      this._render();
    }

    _onInput(value) {
      this._query = value.trim();
      this.shadowRoot.querySelector(".clear").hidden = !value;
      this._open = true;
      clearTimeout(this._timer);
      if (this._query.length < MIN_QUERY) {
        this._loadNearby();
        return;
      }
      this._timer = setTimeout(() => this._search(this._query), DEBOUNCE_MS);
    }

    _onKey(ev) {
      if (ev.key === "Escape") {
        if (this._query) {
          ev.target.value = "";
          this._onInput("");
        } else {
          this._close();
          ev.target.blur();
        }
        ev.stopPropagation();
      } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        if (!this._stops.length) return;
        ev.preventDefault();
        const step = ev.key === "ArrowDown" ? 1 : -1;
        this._active = (this._active + step + this._stops.length) % this._stops.length;
        this._render();
      } else if (ev.key === "Enter") {
        const index = this._active >= 0 ? this._active : 0;
        if (this._stops[index]) this._pick(index);
      }
    }

    // The browser's location, if allowed; null makes the integration use the
    // tracked person instead.
    _locate() {
      if (!this._config.use_device_location || !navigator.geolocation || !window.isSecureContext) {
        return Promise.resolve(null);
      }
      return new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({ latitude: pos.coords.latitude, longitude: pos.coords.longitude }),
          () => resolve(null),
          { enableHighAccuracy: false, timeout: LOCATION_TIMEOUT_MS, maximumAge: NEARBY_MAX_AGE_MS },
        );
      });
    }

    _setMessage(label, message) {
      this._stops = [];
      this._label = label;
      this._message = message;
      this._active = -1;
      this._render();
    }

    async _call(params, label, empty) {
      if (!this._hass) return null;
      const request = ++this._request;
      const entry = this._config.entry_id ? { entry_id: this._config.entry_id } : {};
      try {
        const result = await this._hass.callWS({ ...params, ...entry, ...(this._position ?? {}) });
        if (request !== this._request || !this._open) return null;
        this._stops = result.stops;
        this._label = label;
        this._message = result.stops.length ? "" : empty;
        this._active = -1;
        this._render();
        return result.stops;
      } catch (err) {
        if (request === this._request && this._open) {
          this._setMessage(label, `Couldn't search: ${err?.message || err}`);
        }
        return null;
      }
    }

    async _loadNearby() {
      if (this._nearby && Date.now() - this._nearby.time < NEARBY_MAX_AGE_MS) {
        this._request += 1; // drops a search still on its way
        this._stops = this._nearby.stops;
        this._label = "Near you";
        this._message = this._stops.length ? "" : "No stops nearby.";
        this._active = -1;
        this._render();
        return;
      }
      const request = ++this._request;
      this._setMessage("Near you", "Finding stops near you…");
      this._position = await this._locate();
      if (request !== this._request || !this._open) return;
      const stops = await this._call({ type: `${SOURCE}/nearby` }, "Near you", "No stops nearby.");
      if (stops) this._nearby = { time: Date.now(), stops };
    }

    _search(query) {
      if (!this._open || query !== this._query) return;
      this._setMessage("Results", "Searching…");
      this._call({ type: `${SOURCE}/search`, query }, "Results", "No stops found.");
    }

    _pick(index) {
      const stop = this._stops[index];
      if (!stop) return;
      // A floating list would cover the stop's dialog.
      this._close();
      this.shadowRoot.querySelector("input").blur();
      if (window.dbfCore) {
        window.dbfCore.showStop(stop);
      } else if (stop.url) {
        window.open(stop.url, "_blank", "noopener");
      }
    }

    _render() {
      const results = this.shadowRoot?.querySelector(".results");
      if (!results) return;
      const input = this.shadowRoot.querySelector("input");
      input.setAttribute("aria-expanded", String(this._open));
      if (FLOATING) this._float(results);
      else results.hidden = !this._open;
      if (!this._open) return;
      const items = this._stops.map((stop, index) => {
        const distance = stop.distance_km != null ? `${stop.distance_km} km` : "";
        return `
          <li data-index="${index}" role="option" class="${index === this._active ? "active" : ""}"
            aria-selected="${index === this._active}">
            ${stop.entity_picture ? `<img src="${esc(stop.entity_picture)}" alt="">` : "<span></span>"}
            <div class="name">${esc(stop.name)}</div>
            <div class="meta">${esc(distance)}<span class="net">${esc(stop.networks?.length > 1 ? stop.networks.join(" + ") : stop.network)}</span></div>
          </li>`;
      });
      results.innerHTML = `
        <div class="label">${esc(this._label)}</div>
        ${this._message ? `<div class="empty">${esc(this._message)}</div>` : ""}
        ${items.length ? `<ul role="listbox">${items.join("")}</ul>` : ""}`;
      results.querySelector("li.active")?.scrollIntoView({ block: "nearest" });
    }

    // Shows or hides the floating list. While open it follows the card every
    // frame: scrolling inside Home Assistant's shadow DOM, the sidebar and
    // cards loading above move it without any event reaching the window.
    _float(results) {
      const shown = results.matches(":popover-open");
      this.shadowRoot.querySelector("ha-card").classList.toggle("open", this._open);
      if (this._open && !shown) {
        results.showPopover();
        this._placed = "";
        this._follow();
      } else if (!this._open && shown) {
        results.hidePopover();
        this._stopFollowing();
      }
    }

    _follow() {
      const results = this.shadowRoot?.querySelector(".results");
      const card = this.shadowRoot?.querySelector("ha-card");
      if (!this._open || !results?.matches(":popover-open") || !card) {
        this._stopFollowing();
        return;
      }
      const rect = card.getBoundingClientRect();
      // The visual viewport shrinks for the on-screen keyboard; innerHeight
      // doesn't on iOS.
      const view = window.visualViewport;
      const bottom = view ? view.offsetTop + view.height : window.innerHeight;
      const height = Math.max(120, bottom - rect.bottom - 8);
      const placed = `${rect.left},${rect.bottom},${rect.width},${height}`;
      if (placed !== this._placed) {
        this._placed = placed;
        results.style.left = `${rect.left}px`;
        results.style.top = `${rect.bottom}px`;
        results.style.width = `${rect.width}px`;
        results.style.maxHeight = `${height}px`;
      }
      this._frame = requestAnimationFrame(() => this._follow());
    }

    _stopFollowing() {
      cancelAnimationFrame(this._frame);
      this._frame = 0;
    }
  }

  customElements.define("spdbf-stop-search-card", SpdbfStopSearchCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "spdbf-stop-search-card",
    name: "spdbf stop search",
    description: "Find DB and BVG stops by name or near you, and open their live departures.",
    preview: true,
    documentationURL: "https://github.com/Drakknar/ha-spdbf-nearby#stop-search-card",
  });
})();
