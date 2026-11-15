// Connection to couchd. Keeps a live copy of state, re-dispatches bus messages, and sends
// commands.
//
//   import { Couch } from "./js/couch.js";
//   const couch = new Couch();
//   couch.addEventListener("state", (e) => render(e.detail.state));
//   couch.addEventListener("toast", (e) => showToast(e.detail));
//   couch.addEventListener("key", (e) => handleKey(e.detail.key));
//   couch.connect();
//   await couch.command("audio.set_output", { output: "jack" });  // throws on failure
//
// Message format: docs/protocol.md.

const MIN_RETRY_MS = 500;
const MAX_RETRY_MS = 5000;

export class CommandFailed extends Error {}

export class Couch extends EventTarget {
  constructor(url = `ws://${location.host}/ws`) {
    super();
    this.url = url;
    this.state = {};
    this.connected = false;
    this._ws = null;
    this._retry = MIN_RETRY_MS;
    this._nextId = 1;
    this._pending = new Map();
  }

  connect() {
    const ws = new WebSocket(this.url);
    this._ws = ws;
    ws.addEventListener("open", () => {
      this._retry = MIN_RETRY_MS;
      this._setConnected(true);
    });
    ws.addEventListener("message", (e) => this._onMessage(e.data));
    ws.addEventListener("close", () => {
      this._ws = null;
      this._setConnected(false);
      for (const { reject } of this._pending.values()) {
        reject(new CommandFailed("Lost connection to couchd"));
      }
      this._pending.clear();
      setTimeout(() => this.connect(), this._retry);
      this._retry = Math.min(this._retry * 2, MAX_RETRY_MS);
    });
  }

  command(name, args = {}) {
    if (!this.connected) {
      return Promise.reject(new CommandFailed("Not connected to couchd"));
    }
    const id = this._nextId++;
    this._ws.send(JSON.stringify({ type: "command", id, name, args }));
    return new Promise((resolve, reject) => this._pending.set(id, { resolve, reject }));
  }

  _onMessage(raw) {
    if (raw === "pong") return;
    const msg = JSON.parse(raw);
    if (msg.type === "result") {
      const pending = this._pending.get(msg.id);
      this._pending.delete(msg.id);
      if (!pending) return;
      if (msg.ok) pending.resolve(msg.data);
      else pending.reject(new CommandFailed(msg.error));
    } else if (msg.type === "state") {
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
