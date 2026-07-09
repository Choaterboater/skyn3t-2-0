import { useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Fetch wrapper for the SkyN3t control-plane API.
// All endpoints are mounted under /api/* by the FastAPI app. The dashboard is
// served from the same origin (or proxied in dev via vite.config.js), so we
// use relative URLs.
// ---------------------------------------------------------------------------

const BASE = "/api";
const AUTH_TOKEN_STORAGE_KEY = "skyn3t_token";
const AUTH_TOKEN_EVENT = "skyn3t:auth-token-changed";

function storedAuthToken() {
  return typeof localStorage !== "undefined"
    ? localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)
    : null;
}

export function saveAuthToken(token) {
  const next = String(token || "").trim();
  if (typeof localStorage !== "undefined") {
    if (next) localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, next);
    else localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  }
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_TOKEN_EVENT));
  }
}

function authHeaders() {
  // Optional bearer token persisted by the Settings page.
  const token = storedAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function bearerSubprotocol(token) {
  const bytes = new TextEncoder().encode(String(token || ""));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
  return `skyn3t-bearer.${encoded}`;
}

export async function apiFetch(path, options = {}) {
  const url = path.startsWith("/api") ? path : `${BASE}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch (_) {
      /* non-json error body */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// Convenience query function factory for @tanstack/react-query.
export function queryFn(path) {
  return () => apiFetch(path);
}

export async function apiPost(path, body) {
  return apiFetch(path, { method: "POST", body: JSON.stringify(body || {}) });
}

// ---------------------------------------------------------------------------
// WebSocket hook for the live event stream at /ws.
// Returns { status, events, last, send } and auto-reconnects with backoff.
// The server is expected to push JSON event frames matching skyn3t.core.events
// Event.to_dict() — { type, source, payload, id, timestamp, correlation_id }.
// ---------------------------------------------------------------------------

export function useEventStream({ maxEvents = 200 } = {}) {
  const [status, setStatus] = useState("connecting");
  const [events, setEvents] = useState([]);
  const [last, setLast] = useState(null);
  const wsRef = useRef(null);
  const sendQueue = useRef([]);
  const [authRevision, setAuthRevision] = useState(0);

  useEffect(() => {
    const refreshAuth = () => setAuthRevision((revision) => revision + 1);
    const onStorage = (event) => {
      if (!event.key || event.key === AUTH_TOKEN_STORAGE_KEY) refreshAuth();
    };
    window.addEventListener(AUTH_TOKEN_EVENT, refreshAuth);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(AUTH_TOKEN_EVENT, refreshAuth);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  useEffect(() => {
    let closedByUser = false;
    let retry = 0;
    let timer = null;
    let activeSocket = null;

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      // Browsers can't set headers on a WebSocket, so carry the bearer token as
      // a subprotocol (Sec-WebSocket-Protocol) rather than a ?token= query param
      // — the query param leaks the token into uvicorn/proxy access logs.
      const token = storedAuthToken();
      const url = `${proto}://${window.location.host}/ws`;
      let ws;
      try {
        ws = token
          ? new WebSocket(url, [bearerSubprotocol(token)])
          : new WebSocket(url);
      } catch (_) {
        scheduleReconnect();
        return;
      }
      activeSocket = ws;
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (closedByUser || wsRef.current !== ws) {
          ws.close();
          return;
        }
        retry = 0;
        setStatus("open");
        // Flush anything queued while disconnected.
        sendQueue.current.forEach((m) => ws.send(m));
        sendQueue.current = [];
      };

      ws.onmessage = (msg) => {
        if (closedByUser || wsRef.current !== ws) return;
        let evt;
        try {
          evt = JSON.parse(msg.data);
        } catch (_) {
          return;
        }
        // The hub wraps frames as {event:{…}} — unwrap to the bare Event shape
        // the UI expects (e.type/e.payload/e.source/e.timestamp).
        if (evt && evt.event) evt = evt.event;
        setLast(evt);
        setEvents((prev) => {
          const next = [...prev, evt];
          return next.length > maxEvents
            ? next.slice(next.length - maxEvents)
            : next;
        });
      };

      ws.onerror = () => {
        if (wsRef.current === ws) setStatus("error");
      };

      ws.onclose = () => {
        if (wsRef.current !== ws) return;
        wsRef.current = null;
        activeSocket = null;
        if (closedByUser) return;
        setStatus("closed");
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (closedByUser) return;
      retry += 1;
      const delay = Math.min(1000 * 2 ** retry, 15000);
      timer = setTimeout(connect, delay);
    }

    connect();

    return () => {
      closedByUser = true;
      if (timer) clearTimeout(timer);
      const socket = activeSocket;
      activeSocket = null;
      if (wsRef.current === socket) wsRef.current = null;
      if (socket) socket.close();
    };
  }, [maxEvents, authRevision]);

  function send(obj) {
    const data = typeof obj === "string" ? obj : JSON.stringify(obj);
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(data);
    } else {
      sendQueue.current.push(data);
    }
  }

  return { status, events, last, send };
}
