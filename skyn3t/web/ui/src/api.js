import { useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Fetch wrapper for the SkyN3t control-plane API.
// All endpoints are mounted under /api/* by the FastAPI app. The dashboard is
// served from the same origin (or proxied in dev via vite.config.js), so we
// use relative URLs.
// ---------------------------------------------------------------------------

const BASE = "/api";

function authHeaders() {
  // Optional bearer token persisted by the Settings page.
  const token =
    typeof localStorage !== "undefined"
      ? localStorage.getItem("skyn3t_token")
      : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
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

  useEffect(() => {
    let closedByUser = false;
    let retry = 0;
    let timer = null;

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      // Browsers can't set headers on a WebSocket, so pass the bearer token as a
      // query param when one is configured (the server accepts ?token=).
      const token =
        typeof localStorage !== "undefined"
          ? localStorage.getItem("skyn3t_token")
          : null;
      const qs = token ? `?token=${encodeURIComponent(token)}` : "";
      const url = `${proto}://${window.location.host}/ws${qs}`;
      let ws;
      try {
        ws = new WebSocket(url);
      } catch (_) {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        retry = 0;
        setStatus("open");
        // Flush anything queued while disconnected.
        sendQueue.current.forEach((m) => ws.send(m));
        sendQueue.current = [];
      };

      ws.onmessage = (msg) => {
        let evt;
        try {
          evt = JSON.parse(msg.data);
        } catch (_) {
          return;
        }
        setLast(evt);
        setEvents((prev) => {
          const next = [...prev, evt];
          return next.length > maxEvents
            ? next.slice(next.length - maxEvents)
            : next;
        });
      };

      ws.onerror = () => {
        setStatus("error");
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (closedByUser) return;
        setStatus("closed");
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      retry += 1;
      const delay = Math.min(1000 * 2 ** retry, 15000);
      timer = setTimeout(connect, delay);
    }

    connect();

    return () => {
      closedByUser = true;
      if (timer) clearTimeout(timer);
      if (wsRef.current) wsRef.current.close();
    };
  }, [maxEvents]);

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
