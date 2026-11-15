// Connection to couchd. Keeps a live copy of state and re-dispatches bus messages.
//
//   import { Couch } from "./js/couch.js";
//   const couch = new Couch();
//   couch.addEventListener("state", (e) => render(e.detail.state));
//   couch.addEventListener("toast", (e) => showToast(e.detail));
//   couch.addEventListener("key", (e) => handleKey(e.detail.key));
//   couch.connect();
//
// Message format: docs/protocol.md.

const MIN_RETRY_MS = 500;
const MAX_RETRY_MS = 5000;

export class Couch extends EventTarget {
  constructor(url = `ws://${location.host}/ws`) {
    super();
    this.url = url;
    this.state = {};
    this.connected = false;
    this._retry = MIN_RETRY_MS;
  }

  connect() {
    const ws = new WebSocket(this.url);
    ws.addEventListener("open", () => {
      this._retry = MIN_RETRY_MS;
      this._setConnected(true);
    });
    ws.addEventListener("message", (e) => this._onMessage(e.data));
    ws.addEventListener("close", () => {
      this._setConnected(false);
      setTimeout(() => this.connect(), this._retry);
      this._retry = Math.min(this._retry * 2, MAX_RETRY_MS);
    });
  }

  _onMessage(raw) {
    if (raw === "pong") return;
    const msg = JSON.parse(raw);
    if (msg.type === "state") {
      if (msg.full) {
        this.state = msg.data;
      } else {
        for (const [section, values] of Object.entries(msg.data)) {
          this.state[section] = { ...this.state[section], ...values };
        }
      }
      this._emit("state", { state: this.state, patch: msg.full ? null : msg.data });
    } else {
      this._emit(msg.type, msg.data);
    }
  }

  _setConnected(connected) {
    if (this.connected === connected) return;
    this.connected = connected;
    this._emit("connection", { connected });
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
