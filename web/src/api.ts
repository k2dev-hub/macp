import type {
  Agent,
  LlmProvider,
  AlertRow,
  Approval,
  EventItem,
  Lease,
  Metrics,
  Overview,
  Task,
  TaskDetail,
  DocumentMeta,
} from "./types";

const TOKEN_KEY = "macp_admin_token";
const PROJECT_KEY = "macp_project";

export function getToken(): string {
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function getProject(): string {
  return window.localStorage.getItem(PROJECT_KEY) ?? "demo";
}

export function setProject(value: string) {
  window.localStorage.setItem(PROJECT_KEY, value || "demo");
}

export function setToken(value: string) {
  if (value) {
    window.localStorage.setItem(TOKEN_KEY, value);
  } else {
    window.localStorage.removeItem(TOKEN_KEY);
  }
}

async function get<T>(path: string, auth = false): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-MACP-Project": getProject(),
  };
  const token = getToken();
  if (auth) {
    if (!token) throw new Error("请先在右上角配置管理员 Token");
    headers.Authorization = `Bearer ${token}`;
  }
  const response = await fetch(path, { headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(errorText(path, response.status, body));
  }
  return (await response.json()) as T;
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getToken();
  if (!token) throw new Error("请先在右上角配置管理员 Token");
  const response = await fetch(path, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-MACP-Project": getProject(),
      Authorization: `Bearer ${token}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(errorText(path, response.status, payload));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function errorText(path: string, status: number, payload: unknown): string {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return `${path}: HTTP ${status} ${detail}`;
  if (detail) return `${path}: HTTP ${status} ${JSON.stringify(detail)}`;
  return `${path}: HTTP ${status}`;
}

export const api = {
  overview: () => get<Overview>("/v1/overview"),
  tasks: () => get<Task[]>("/v1/tasks"),
  task: (code: string) => get<TaskDetail>(`/v1/tasks/${code}`),
  agents: () => get<Agent[]>("/v1/agents"),
  metrics: () => get<Metrics>("/v1/metrics"),
  events: (since = 0) => get<EventItem[]>(`/v1/events?since=${since}&limit=80`),
  alerts: () => get<AlertRow[]>("/v1/alerts?state=open"),
  leases: () => get<Lease[]>("/v1/leases?state=active"),
  approvals: (pending = true) => get<Approval[]>(`/v1/approvals?pending=${pending}`),

  createTask: (body: Record<string, unknown>) => send<Task>("POST", "/v1/tasks", body),
  dispatch: (code: string, agent: string) =>
    send<{ assignment_id: string }>("POST", `/v1/tasks/${code}/dispatch`, { agent }),
  requeue: (code: string) => send<{ state: string }>("POST", `/v1/tasks/${code}/requeue`),
  review: (code: string, body: Record<string, unknown>) =>
    send<{ state: string }>("POST", `/v1/tasks/${code}/reviews`, body),
  enqueueMerge: (code: string) =>
    send<{ merge_id: string; state: string }>("POST", "/v1/merges", { task: code }),
  processMerge: (mergeId: string) =>
    send<{ state: string; reason?: string }>("POST", `/v1/merges/${mergeId}/process`),
  requestApproval: (body: Record<string, unknown>) =>
    send<{ id: string }>("POST", "/v1/approvals", body),
  decideApproval: (id: string, decision: string, rationale: string) =>
    send<{ decision: string }>("POST", `/v1/approvals/${id}/decide`, { decision, rationale }),
  renewLease: (id: string) =>
    send<{ expires_at: string }>("POST", `/v1/leases/${id}/renew`, { minutes: 30 }),
  releaseLease: (id: string) => send<{ state: string }>("POST", `/v1/leases/${id}/release`),
  ackAlert: (id: string, state: "acked" | "cleared") =>
    send<{ state: string }>("POST", `/v1/alerts/${id}/ack`, { state }),

  uploadDocument: async (file: File): Promise<DocumentMeta> => {
    const token = getToken();
    if (!token) throw new Error("请先在右上角配置管理员 Token");
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/v1/documents", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "X-MACP-Project": getProject(),
        Authorization: `Bearer ${token}`,
      },
      body: form,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(errorText("/v1/documents", response.status, payload));
    }
    return (await response.json()) as DocumentMeta;
  },
  documentText: (id: string, offset = 0, limit = 20000) =>
    get<{
      id: string;
      filename: string;
      text: string;
      char_count: number;
      next_offset: number | null;
    }>(`/v1/documents/${id}/text?offset=${offset}&limit=${limit}`, true),
  llmProviders: () => get<LlmProvider[]>("/v1/admin/llm-providers", true),
  saveLlmProvider: (body: Record<string, unknown>) =>
    send<{ id: string }>("POST", "/v1/admin/llm-providers", body),
  testLlmProvider: (id: string) =>
    send<{ ok: boolean; status: number; models?: string[]; error?: string }>(
      "POST",
      `/v1/admin/llm-providers/${id}/test`,
    ),
  disableLlmProvider: (id: string) =>
    send<{ enabled: boolean }>("DELETE", `/v1/admin/llm-providers/${id}`),
  projects: () =>
    get<Array<{ id: string; code: string; name: string }>>("/v1/projects", true),
  authConfig: () =>
    get<{ issuer: string; client_id: string; scope: string }>("/v1/auth/config"),
  exchangeCode: (code: string, redirectUri: string) =>
    fetch("/v1/auth/token", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-MACP-Project": getProject(),
      },
      body: JSON.stringify({ code, redirect_uri: redirectUri }),
    }).then(async (response) => {
      if (!response.ok) {
        throw new Error(`SSO 登录失败: HTTP ${response.status}`);
      }
      return (await response.json()) as { access_token: string };
    }),
};
