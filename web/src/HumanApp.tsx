import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Avatar, Badge, Button, Card, Col, Descriptions, Divider, Drawer, Empty,
  Form, Input, Layout, List, Menu, Modal, Popconfirm, Row, Select, Space, Table, Tabs,
  Tag, Timeline, Typography, Upload, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  AlertOutlined, ApiOutlined, CheckCircleOutlined, CloudServerOutlined,
  DashboardOutlined, FileTextOutlined, KeyOutlined, LockOutlined, LoginOutlined, LogoutOutlined,
  MenuFoldOutlined, MenuUnfoldOutlined, PlusOutlined, ReloadOutlined, RobotOutlined,
  SafetyCertificateOutlined, TeamOutlined, ThunderboltOutlined, UnorderedListOutlined,
  UploadOutlined, UserOutlined, ProjectOutlined, InboxOutlined, PartitionOutlined,
  DeploymentUnitOutlined, ClusterOutlined, ExperimentOutlined, ReadOutlined,
} from "@ant-design/icons";
import type { EChartsOption } from "echarts";
import { api, getProject, getToken, setProject, setToken } from "./api";

// 深链：?token=… 写入本地存储（只做一次），?page=… 作为初始页
const DEEP_LINK = new URLSearchParams(window.location.search);
if (DEEP_LINK.get("token")) window.localStorage.setItem("macp_admin_token", DEEP_LINK.get("token") as string);
import EChart from "./EChart";
import { AdminPage, adminPageKeys } from "./AdminPages";
import type { UiPageKey } from "./AdminPages";
import type {
  Agent, AlertRow, Approval, DocumentMeta, EventItem, Lease, LlmProvider,
  Metrics, Overview, Task, TaskDetail, TaskState,
} from "./types";

const { Header, Content, Sider } = Layout;
const { Title, Text, Paragraph } = Typography;

const LABELS: Record<string, string> = {
  ready: "待派单", assigned: "已派单", in_progress: "执行中", blocked: "阻塞",
  review: "待验收", accepted: "已验收", done: "完成", reopened: "重开",
  cancelled: "已取消", failed: "失败", merged: "已合并",
};
const COLORS: Record<string, string> = {
  ready: "default", assigned: "processing", in_progress: "processing", blocked: "error",
  review: "warning", accepted: "success", done: "success", reopened: "warning",
  cancelled: "default", failed: "error", merged: "success",
};
const AGENT_LABEL: Record<string, string> = {
  idle: "空闲", working: "工作中", waiting_user: "等待人工", blocked: "阻塞",
  offline: "离线", stalled: "失联", dead: "失联",
};
const AGENT_BADGE: Record<string, "success" | "processing" | "warning" | "error" | "default"> = {
  idle: "success", working: "processing", waiting_user: "warning",
  blocked: "error", offline: "default", stalled: "error", dead: "error",
};
const STATE_ORDER = ["ready", "assigned", "in_progress", "blocked", "review", "accepted", "merged"];
const KEY_EVENTS = new Set([
  "task.created", "task.assigned", "task.started", "task.blocked", "task.requeued",
  "task.completed", "task.rejected", "task.reworked", "task.accepted", "task.merged",
  "task.reopened", "task.cancelled", "task.reconciled", "gate.passed", "gate.failed",
  "review.accepted", "review.rejected", "merge.queued", "merge.merged", "merge.conflict",
  "merge.reverted", "merge.requeued", "approval.requested", "approval.approved",
  "approval.denied", "alert.opened", "alert.cleared", "budget.exhausted",
]);
const ADMIN_PAGES = new Set([
  "approvals", "leases", "alerts", "llm",
  "projects", "artifacts", "chain", "agents-admin", "workload", "models",
]);
const BAR_COLOR: Record<string, string> = {
  ready: "#98a2b3", assigned: "#2563eb", in_progress: "#4f46e5", blocked: "#e5484d",
  review: "#d97706", accepted: "#059669", merged: "#0f766e",
};

function time(value?: string | null) {
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false }) : "—";
}
function datetime(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}
function stateTag(state: string) {
  return <Badge status={(COLORS[state] ?? "default") as never} text={LABELS[state] ?? state} />;
}
function priorityTag(priority: string) {
  return <Tag color={priority === "P0" ? "red" : priority === "P1" ? "orange" : "default"}>{priority}</Tag>;
}
function eventLabel(event: { type: string; payload?: Record<string, unknown> }) {
  const p = event.payload || {};
  const base: Record<string, string> = {
    "task.created": "创建任务", "task.assigned": "派发", "task.started": "开始执行",
    "task.blocked": "阻塞", "task.requeued": "重新排队", "task.completed": "提交回执",
    "task.rejected": "验收驳回", "task.reworked": "自动返工", "task.accepted": "验收通过",
    "task.merged": "已合并", "task.reopened": "重开", "task.cancelled": "取消",
    "assignment.offered": "派发待认领", "assignment.claimed": "Worker 认领",
    "lease.renewed": "租约续期", "lease.released": "租约释放",
    "gate.passed": "门禁通过", "gate.failed": "门禁失败",
    "review.accepted": "Review 通过", "review.rejected": "Review 驳回",
    "merge.queued": "进入合并", "merge.merged": "合并完成", "merge.conflict": "合并冲突",
    "merge.reverted": "合并回滚", "approval.requested": "请求审批",
    "approval.approved": "审批通过", "approval.denied": "审批拒绝",
    "alert.opened": "告警", "alert.cleared": "告警清除", "tool.called": "调用工具",
    "tool.succeeded": "工具成功", "tool.failed": "工具失败", "model.usage": "模型用量",
  };
  let title = base[event.type] ?? event.type;
  const extra = p.tool || p.gate || p.model;
  if (extra && ["tool.called", "tool.failed", "gate.passed", "gate.failed", "model.usage"].includes(event.type)) {
    title += ` · ${extra}`;
  }
  const detail = (p.reason as string) || (p.detail as string) || (p.agent as string) ||
    (p.receipt_id as string) || (p.title as string) || "";
  return { title, detail: String(detail) };
}
function decodeIdentity(token: string): string {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.email || payload.name || payload.sub || "";
  } catch {
    return "";
  }
}

function agentCount(agents: Agent[], status?: string) {
  return status ? agents.filter((a) => a.status === status).length : agents.length;
}

export default function HumanApp() {
  const secure = window.location.protocol === "https:";
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<TaskDetail | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [page, setPage] = useState(DEEP_LINK.get("page") || "overview");
  const [collapsed, setCollapsed] = useState(false);
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const [eventFilter, setEventFilter] = useState("key");
  const [selectedTaskCodes, setSelectedTaskCodes] = useState<string[]>([]);
  const [token, setTokenState] = useState(getToken());
  const [authMethod, setAuthMethodState] = useState(window.localStorage.getItem("macp_auth_method") || (getToken() ? "token" : ""));
  const [tokenOpen, setTokenOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [project, setProjectState] = useState(getProject());
  const [projects, setProjects] = useState<Array<{ code: string; name: string }>>([]);
  const [uploadedDocs, setUploadedDocs] = useState<DocumentMeta[]>([]);
  const [uploading, setUploading] = useState(false);
  const [docOpen, setDocOpen] = useState(false);
  const [docText, setDocText] = useState("");
  const [docName, setDocName] = useState("");
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [leases, setLeases] = useState<Lease[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [llmProviders, setLlmProviders] = useState<LlmProvider[]>([]);
  const [llmOpen, setLlmOpen] = useState(false);
  const [editingLlm, setEditingLlm] = useState<LlmProvider | null>(null);
  const [mergeIds, setMergeIds] = useState<Record<string, string>>({});
  const [rejectFindings, setRejectFindings] = useState("");
  const [taskForm] = Form.useForm();
  const [tokenForm] = Form.useForm();
  const [llmForm] = Form.useForm();
  const refreshTimer = useRef<number | null>(null);

  const refreshAdmin = useCallback(async () => {
    if (!token) {
      setAlerts([]); setLeases([]); setApprovals([]); setLlmProviders([]);
      return;
    }
    try {
      const [a, l, p, llm] = await Promise.all([api.alerts(), api.leases(), api.approvals(), api.llmProviders()]);
      setAlerts(a); setLeases(l); setApprovals(p); setLlmProviders(llm);
    } catch { /* keep views usable */ }
  }, [token]);

  const load = useCallback(async () => {
    try {
      const [o, t, a, m] = await Promise.all([api.overview(), api.tasks(), api.agents(), api.metrics()]);
      setOverview(o); setTasks(t); setAgents(a); setMetrics(m); setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setLoading(false); }
  }, []);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => { void load(); void refreshAdmin(); }, 250);
  }, [load, refreshAdmin]);

  useEffect(() => {
    void load(); void refreshAdmin();
    const timer = window.setInterval(() => { void load(); void refreshAdmin(); }, 5000);
    return () => { window.clearInterval(timer); if (refreshTimer.current) window.clearTimeout(refreshTimer.current); };
  }, [load, refreshAdmin]);

  useEffect(() => {
    if (!token) return;
    const loadProjects = () => {
      void api.projects().then(setProjects).catch(() => setProjects([]));
    };
    loadProjects();
    // 新建/改名后由管理页发事件，侧栏跟着刷新，不用整页重载
    const onChanged = () => loadProjects();
    window.addEventListener("macp:projects-changed", onChanged);
    return () => window.removeEventListener("macp:projects-changed", onChanged);
  }, [token]);

  useEffect(() => {
    const initial = async () => {
      try {
        const items = await api.events(0);
        setEvents((prev) => {
          const map = new Map<number, EventItem>();
          for (const item of [...items, ...prev]) map.set(item.seq, item);
          return Array.from(map.values()).sort((a, b) => b.seq - a.seq).slice(0, 100);
        });
      } catch { /* stream fills */ }
    };
    void initial();
    const es = new EventSource(`/v1/events/stream?since=0&project=${getProject()}`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    const handle = (event: MessageEvent) => {
      try {
        const item = JSON.parse(event.data) as EventItem;
        setLastEventAt(item.occurred_at);
        setEvents((prev) => prev.some((e) => e.seq === item.seq) ? prev : [item, ...prev].slice(0, 100));
        if (KEY_EVENTS.has(item.type)) scheduleRefresh();
      } catch { /* ignore */ }
    };
    const types = ["task.created", "task.assigned", "task.started", "task.blocked", "task.requeued",
      "task.completed", "task.rejected", "task.reworked", "task.accepted", "task.merged",
      "task.reopened", "task.cancelled", "assignment.offered", "assignment.claimed",
      "assignment.expired", "assignment.revoked", "assignment.closed", "attempt.created",
      "lease.renewed", "lease.released", "lease.conflict", "gate.passed", "gate.failed",
      "review.accepted", "review.rejected", "merge.queued", "merge.merged", "merge.conflict",
      "merge.reverted", "merge.requeued", "approval.requested", "approval.approved",
      "approval.denied", "budget.exhausted", "alert.opened", "alert.cleared", "tool.called",
      "tool.succeeded", "tool.failed", "model.usage", "token.issued", "token.revoked"];
    for (const type of types) es.addEventListener(type, handle as EventListener);
    return () => es.close();
  }, [scheduleRefresh]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code) return;
    const expected = window.localStorage.getItem("macp_oidc_state");
    if (!expected || params.get("state") !== expected) { message.error("SSO state 校验失败"); return; }
    void (async () => {
      try {
        const result = await api.exchangeCode(code, window.location.origin + "/");
        setToken(result.access_token); setTokenState(result.access_token);
        window.localStorage.setItem("macp_auth_method", "sso"); setAuthMethodState("sso");
        window.localStorage.removeItem("macp_oidc_state");
        window.history.replaceState({}, "", window.location.pathname);
        message.success("SSO 登录成功");
        await load(); await refreshAdmin();
      } catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
    })();
  }, [load, refreshAdmin]);

  const loginWithSso = async () => {
    try {
      const config = await api.authConfig();
      const state = Math.random().toString(36).slice(2);
      window.localStorage.setItem("macp_oidc_state", state);
      const origin = secure ? window.location.origin : `https://${window.location.hostname}:9443`;
      const params = new URLSearchParams({
        client_id: config.client_id, redirect_uri: origin + "/",
        response_type: "code", scope: config.scope, state,
      });
      window.location.href = `${config.issuer}/authorize?${params.toString()}`;
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
  };

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn(); message.success(`${label}成功`);
      await load(); await refreshAdmin();
      if (selected) setSelected(await api.task(selected.code));
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };

  const openTask = async (code: string) => {
    try { setSelected(await api.task(code)); setRejectFindings(""); setDrawerOpen(true); }
    catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
  };

  const createTask = async () => {
    const values = await taskForm.validateFields();
    await act("创建任务", async () => {
      await api.createTask({
        ...values,
        documents: uploadedDocs.map((doc) => doc.id),
        budget: { max_tool_calls: 40, max_model_calls: 30, max_tokens: 400000, max_cost: values.max_cost ?? 2, max_wall_seconds: 900 },
      });
      setCreateOpen(false); taskForm.resetFields(); setUploadedDocs([]);
    });
  };

  const uploadDoc = async (file: File) => {
    setUploading(true);
    try {
      const uploaded = await api.uploadDocument(file);
      setUploadedDocs((prev) => [...prev, uploaded]);
      message.success(`${uploaded.filename} 已解析 ${uploaded.char_count} 字符`);
      if (uploaded.warnings?.length) message.warning(uploaded.warnings.join("; "));
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
    finally { setUploading(false); }
    return false;
  };

  const showDoc = async (doc: DocumentMeta) => {
    try {
      const result = await api.documentText(doc.id);
      setDocName(doc.filename); setDocText(result.text); setDocOpen(true);
    } catch (err) { message.error(err instanceof Error ? err.message : String(err)); }
  };

  const dispatchTask = async () => {
    if (!selected) return;
    const agent = (selected as unknown as { _agent?: string })._agent;
    if (!agent) { message.warning("请选择空闲 Agent"); return; }
    await act("派单", () => api.dispatch(selected.code, agent));
  };

  const reviewTask = async (decision: "accept" | "reject") => {
    if (!selected?.receipt) { message.warning("该任务没有 Receipt"); return; }
    if (decision === "reject" && !rejectFindings.trim()) { message.warning("驳回必须填写 findings"); return; }
    await act(decision === "accept" ? "验收" : "驳回", () =>
      api.review(selected.code, {
        receipt_id: selected.receipt?.code, decision,
        findings: decision === "reject" ? [rejectFindings.trim()] : [],
        reviewer: "coordinator-1",
      }));
  };

  const enqueueMerge = async () => {
    if (!selected) return;
    await act("加入合并队列", async () => {
      const result = await api.enqueueMerge(selected.code);
      setMergeIds((prev) => ({ ...prev, [selected.code]: result.merge_id }));
    });
  };
  const processMerge = async () => {
    const mergeId = selected && mergeIds[selected.code];
    if (!mergeId) { message.warning("先加入合并队列"); return; }
    await act("合并", () => api.processMerge(mergeId));
  };
  const finishBatch = async (ok: number, failed: number, label: string) => {
    setSelectedTaskCodes([]);
    await load(); await refreshAdmin();
    if (failed) message.warning(`${label}完成：成功 ${ok}，失败 ${failed}`);
    else message.success(`${label}完成：${ok} 个任务`);
  };

  const batchDispatch = async () => {
    const ready = selectedTasks.filter((t) => t.state === "ready");
    if (!ready.length) { message.warning("选中任务里没有待派单任务"); return; }
    let idle = [...idleAgents];
    if (!idle.length) { message.warning("当前没有空闲 Agent"); return; }
    setBusy(true);
    let ok = 0, failed = 0;
    for (const task of ready) {
      if (!idle.length) { failed += 1; continue; }
      const agent = idle.shift()!.code;
      try { await api.dispatch(task.code, agent); ok += 1; } catch { failed += 1; }
    }
    setBusy(false);
    await finishBatch(ok, failed, "批量派单");
  };

  const batchRequeue = async () => {
    const targets = selectedTasks.filter((t) => ["blocked", "reopened", "failed"].includes(t.state));
    if (!targets.length) { message.warning("选中任务里没有可重新排队的任务"); return; }
    setBusy(true);
    let ok = 0, failed = 0;
    for (const task of targets) {
      try { await api.requeue(task.code); ok += 1; } catch { failed += 1; }
    }
    setBusy(false);
    await finishBatch(ok, failed, "批量重新排队");
  };

  const batchMerge = async () => {
    const targets = selectedTasks.filter((t) => t.state === "accepted");
    if (!targets.length) { message.warning("选中任务里没有已验收任务"); return; }
    setBusy(true);
    let ok = 0, failed = 0;
    for (const task of targets) {
      try { await api.enqueueMerge(task.code); ok += 1; } catch { failed += 1; }
    }
    setBusy(false);
    await finishBatch(ok, failed, "批量加入合并队列");
  };

  const requestApproval = async () => {
    if (!selected) return;
    await act("请求审批", () => api.requestApproval({
      task: selected.code, action: "merge", risk: "high",
      options: ["approved", "denied"], default_option: "denied", ttl_hours: 24,
    }));
  };
  const saveLlm = async () => {
    const values = await llmForm.validateFields();
    await act("保存 LLM 配置", async () => {
      await api.saveLlmProvider({ ...values, api_key: values.api_key?.trim() || undefined });
      setLlmOpen(false); setEditingLlm(null); llmForm.resetFields();
    });
  };

  const filteredTasks = useMemo(() => {
    const q = search.toLowerCase().trim();
    return tasks.filter((task) => {
      if (stateFilter !== "all" && task.state !== stateFilter) return false;
      if (!q) return true;
      return `${task.code} ${task.title} ${task.workstream} ${task.assignee ?? ""}`.toLowerCase().includes(q);
    });
  }, [tasks, search, stateFilter]);
  const keyEvents = useMemo(() => events.filter((e) => KEY_EVENTS.has(e.type)), [events]);
  const filteredEvents = useMemo(() => {
    if (eventFilter === "all") return events;
    if (eventFilter === "key") return keyEvents;
    return events.filter((e) => e.type === eventFilter);
  }, [events, keyEvents, eventFilter]);
  const selectedTasks = useMemo(
    () => tasks.filter((t) => selectedTaskCodes.includes(t.code)),
    [tasks, selectedTaskCodes],
  );
  const idleAgents = agents.filter((a) => a.role === "worker" && a.status === "idle");

  const stateChart = useMemo<EChartsOption>(() => {
    const counts = new Map((metrics?.states ?? []).map((s) => [s.state, s.count]));
    const rows = STATE_ORDER.map((s) => ({ s, c: counts.get(s) ?? 0 }));
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 34, right: 12, top: 18, bottom: 26 },
      xAxis: { type: "category", data: rows.map((r) => LABELS[r.s]), axisLabel: { fontSize: 11, color: "#667085" }, axisLine: { lineStyle: { color: "#e4e7ec" } } },
      yAxis: { type: "value", minInterval: 1, axisLabel: { fontSize: 11, color: "#98a2b3" }, splitLine: { lineStyle: { color: "#f2f4f7" } } },
      series: [{ type: "bar", barMaxWidth: 32, data: rows.map((r) => ({ value: r.c, itemStyle: { color: BAR_COLOR[r.s], borderRadius: [6, 6, 0, 0] } })) }],
    };
  }, [metrics]);

  const rateChart = useMemo<EChartsOption>(() => {
    const rows = metrics?.event_rate ?? [];
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 34, right: 12, top: 18, bottom: 26 },
      xAxis: { type: "category", boundaryGap: false, data: rows.map((r) => r.minute), axisLabel: { fontSize: 11, color: "#98a2b3" }, axisLine: { lineStyle: { color: "#e4e7ec" } } },
      yAxis: { type: "value", minInterval: 1, axisLabel: { fontSize: 11, color: "#98a2b3" }, splitLine: { lineStyle: { color: "#f2f4f7" } } },
      series: [{ type: "line", smooth: true, data: rows.map((r) => r.count), symbolSize: 4, lineStyle: { color: "#0ea5a4", width: 2 }, itemStyle: { color: "#0ea5a4" }, areaStyle: { color: "rgba(14,165,164,.10)" } }],
    };
  }, [metrics]);

  const wipChart = useMemo<EChartsOption>(() => {
    const rows = metrics?.workstreams ?? [];
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 66, right: 16, top: 16, bottom: 22 },
      xAxis: { type: "value", minInterval: 1, axisLabel: { fontSize: 11, color: "#98a2b3" }, splitLine: { lineStyle: { color: "#f2f4f7" } } },
      yAxis: { type: "category", data: rows.map((r) => r.name), axisLabel: { fontSize: 11, color: "#667085" }, axisLine: { lineStyle: { color: "#e4e7ec" } } },
      series: [
        { type: "bar", data: rows.map((r) => r.wip_limit), barMaxWidth: 16, barGap: "-100%", itemStyle: { color: "#eef2f6", borderRadius: [0, 6, 6, 0] }, silent: true },
        { type: "bar", data: rows.map((r) => r.wip), barMaxWidth: 16, itemStyle: { color: "#7c3aed", borderRadius: [0, 6, 6, 0] } },
      ],
    };
  }, [metrics]);

  const tasksColumns: ColumnsType<Task> = [
    { title: "任务", dataIndex: "code", width: 280, render: (code, row) => (
      <div><Text strong>{code}</Text><div className="row-sub">{row.title}</div></div>
    )},
    { title: "状态", dataIndex: "state", width: 110, render: (s: TaskState) => stateTag(s) },
    { title: "工作流", dataIndex: "workstream", width: 110 },
    { title: "执行者", dataIndex: "assignee", width: 150, render: (v: string | null) => v ? (
      <Space size={6}><Avatar size={22} style={{ background: "#e0e7ff", color: "#3730a3" }}>{v[0].toUpperCase()}</Avatar><Text>{v}</Text></Space>
    ) : <Text type="secondary">未派单</Text> },
    { title: "优先级", dataIndex: "priority", width: 80, render: priorityTag },
    { title: "更新", dataIndex: "updated_at", width: 100, render: time },
  ];

  const approvalColumns: ColumnsType<Approval> = [
    { title: "任务", dataIndex: "task", width: 110 },
    { title: "动作", dataIndex: "action" },
    { title: "风险", dataIndex: "risk", width: 90, render: (r: string) => <Tag color={r === "high" || r === "critical" ? "red" : "gold"}>{r}</Tag> },
    { title: "申请时间", dataIndex: "requested_at", width: 170, render: datetime },
    { title: "操作", width: 170, render: (_, row) => (
      <Space>
        <Button size="small" type="primary" onClick={() => void act("批准", () => api.decideApproval(row.id, "approved", "控制台批准"))}>批准</Button>
        <Button size="small" danger onClick={() => void act("拒绝", () => api.decideApproval(row.id, "denied", "控制台拒绝"))}>拒绝</Button>
      </Space>
    )},
  ];
  const leaseColumns: ColumnsType<Lease> = [
    { title: "任务", dataIndex: "task", width: 110 },
    { title: "路径", dataIndex: "path_glob" },
    { title: "Agent", dataIndex: "agent", width: 140 },
    { title: "到期", dataIndex: "expires_at", width: 170, render: datetime },
    { title: "操作", width: 160, render: (_, row) => (
      <Space>
        <Button size="small" onClick={() => void act("续租", () => api.renewLease(row.id))}>续租</Button>
        <Popconfirm title="释放该租约？" onConfirm={() => void act("释放", () => api.releaseLease(row.id))}>
          <Button size="small" danger>释放</Button>
        </Popconfirm>
      </Space>
    )},
  ];
  const alertColumns: ColumnsType<AlertRow> = [
    { title: "级别", dataIndex: "severity", width: 90, render: (s: string) => <Tag color={s === "critical" ? "red" : s === "warning" ? "gold" : "blue"}>{s}</Tag> },
    { title: "规则", dataIndex: "code", width: 190 },
    { title: "次数", dataIndex: "count", width: 70 },
    { title: "最近", dataIndex: "last_seen", width: 170, render: datetime },
    { title: "详情", dataIndex: "details", ellipsis: true, render: (d: Record<string, unknown>) => <Text type="secondary">{JSON.stringify(d)}</Text> },
    { title: "操作", width: 150, render: (_, row) => (
      <Space>
        <Button size="small" onClick={() => void act("确认告警", () => api.ackAlert(row.id, "acked"))}>确认</Button>
        <Button size="small" onClick={() => void act("清除告警", () => api.ackAlert(row.id, "cleared"))}>清除</Button>
      </Space>
    )},
  ];
  const llmColumns: ColumnsType<LlmProvider> = [
    { title: "Agent", dataIndex: "agent_code", width: 140 },
    { title: "模型", dataIndex: "model", width: 170 },
    { title: "Base URL", dataIndex: "base_url", ellipsis: true },
    { title: "Key", dataIndex: "api_key_set", width: 90, render: (v: boolean) => v ? <Tag color="green">已配置</Tag> : <Tag>未配置</Tag> },
    { title: "价格/百万", width: 140, render: (_, row) => `${row.price_input_per_1m} / ${row.price_output_per_1m}` },
    { title: "操作", width: 220, render: (_, row) => (
      <Space>
        <Button size="small" onClick={() => void act("测试连接", async () => {
          const result = await api.testLlmProvider(row.id);
          if (result.ok) message.success(`连接成功，模型数 ${result.models?.length ?? 0}`);
          else message.error(`连接失败 ${result.status}: ${result.error ?? ""}`);
        })}>测试</Button>
        <Button size="small" onClick={() => {
          setEditingLlm(row); llmForm.setFieldsValue({ ...row, api_key: "" }); setLlmOpen(true);
        }}>编辑</Button>
        <Popconfirm title="停用该配置？" onConfirm={() => void act("停用 LLM", () => api.disableLlmProvider(row.id))}>
          <Button size="small" danger>停用</Button>
        </Popconfirm>
      </Space>
    )},
  ];

  const actionPanel = () => {
    if (!selected) return null;
    const mergeId = mergeIds[selected.code];
    return (
      <Card size="small" className="action-card" title="下一步">
        <Space wrap>
          {selected.state === "ready" && (
            <>
              <Select placeholder="选择空闲 Agent" style={{ width: 190 }} options={idleAgents.map((a) => ({ value: a.code, label: a.code }))}
                onChange={(value) => setSelected((prev) => prev ? ({ ...prev, _agent: value } as TaskDetail) : prev)} />
              <Button type="primary" loading={busy} onClick={() => void dispatchTask()}>派单</Button>
            </>
          )}
          {selected.state === "review" && selected.receipt && (
            <>
              <Button type="primary" loading={busy} onClick={() => void reviewTask("accept")}>验收通过</Button>
              <Input placeholder="驳回原因 / findings" value={rejectFindings} onChange={(e) => setRejectFindings(e.target.value)} style={{ width: 240 }} />
              <Button danger loading={busy} onClick={() => void reviewTask("reject")}>驳回返工</Button>
            </>
          )}
          {selected.state === "accepted" && (
            <>
              <Button loading={busy} onClick={() => void enqueueMerge()}>加入合并队列</Button>
              {mergeId && <Button type="primary" loading={busy} onClick={() => void processMerge()}>执行合并</Button>}
              <Button loading={busy} onClick={() => void requestApproval()}>请求合并审批</Button>
            </>
          )}
          {(selected.state === "blocked" || selected.state === "reopened" || selected.state === "failed") && (
            <Button type="primary" loading={busy} onClick={() => void act("重新排队", () => api.requeue(selected.code))}>重新排队</Button>
          )}
          {!secure ? <Text type="secondary">写操作请使用 https://{window.location.hostname}:9443</Text> : !token && <Text type="secondary">配置管理员 Token 后可执行写操作</Text>}
        </Space>
      </Card>
    );
  };

  const overviewPage = () => (
    <>
      <Card className="hero" bordered={false}>
        <div className="hero-copy">
          <Text className="eyebrow">MULTI-AGENT COORDINATION</Text>
          <Title level={2}>今天的协作状态</Title>
          <Paragraph>任务从工单派发，经隔离执行、门禁、回执到合并，每步留存证据。</Paragraph>
          <Space wrap>
            {token ? (
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建任务</Button>
            ) : (
              <Button type="primary" icon={<LoginOutlined />} onClick={() => void loginWithSso()}>登录后新建</Button>
            )}
            <Button onClick={() => setPage("tasks")}>查看任务</Button>
            <Button onClick={() => setPage("events")}>实时事件</Button>
          </Space>
        </div>
        <div className="hero-numbers">
          <div><span>执行中</span><strong>{overview?.execution_wip ?? 0}</strong></div>
          <div><span>待验收</span><strong>{overview?.review_debt ?? 0}</strong></div>
          <div><span>阻塞</span><strong>{overview?.blocked ?? 0}</strong></div>
          <div><span>未处理告警</span><strong>{overview?.open_alerts ?? 0}</strong></div>
        </div>
      </Card>

      <Row gutter={[12, 12]} className="section">
        {[
          { label: "执行中 WIP", value: overview?.execution_wip ?? 0, hint: "已派单 / 执行中 / 阻塞", icon: <ThunderboltOutlined />, tone: "blue" },
          { label: "待验收", value: overview?.review_debt ?? 0, hint: "等待验收", icon: <SafetyCertificateOutlined />, tone: "amber" },
          { label: "阻塞", value: overview?.blocked ?? 0, hint: "需要恢复", icon: <AlertOutlined />, tone: "red" },
          { label: "已验收", value: overview?.accepted ?? 0, hint: "等待合并", icon: <CheckCircleOutlined />, tone: "green" },
          { label: "在线 Agent", value: overview?.agents_online ?? 0, hint: "工作中 / 空闲", icon: <RobotOutlined />, tone: "violet" },
          { label: "今日模型成本", value: `¥${(overview?.model_cost_today ?? 0).toFixed(4)}`, hint: "按 token 累计", icon: <CloudServerOutlined />, tone: "teal" },
        ].map((item) => (
          <Col xs={12} sm={8} lg={4} key={item.label}>
            <Card className="metric-card" size="small">
              <div className={`metric-icon ${item.tone}`}>{item.icon}</div>
              <div className="metric-label">{item.label}</div>
              <div className="metric-value">{item.value}</div>
              <div className="metric-hint">{item.hint}</div>
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[14, 14]} className="section">
        <Col xs={24} lg={9}><Card title="任务状态" size="small"><EChart option={stateChart} /></Card></Col>
        <Col xs={24} lg={9}><Card title="事件速率" size="small"><EChart option={rateChart} /></Card></Col>
        <Col xs={24} lg={6}><Card title="工作流负载" size="small"><EChart option={wipChart} /></Card></Col>
      </Row>

      <Row gutter={[14, 14]} className="section">
        <Col xs={24} lg={15}>
          <Card title="最近任务" size="small" extra={<Button type="link" onClick={() => setPage("tasks")}>全部</Button>}>
            <List dataSource={tasks.filter((t) => !["cancelled", "done"].includes(t.state)).slice(0, 6)} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" /> }} renderItem={(task) => (
              <List.Item className="clickable" onClick={() => void openTask(task.code)} extra={priorityTag(task.priority)}>
                <List.Item.Meta avatar={<Avatar shape="square" style={{ background: "#eef2ff", color: "#3730a3" }}>{task.code.slice(-2)}</Avatar>}
                  title={<Space><Text strong>{task.code}</Text><Text>{task.title}</Text></Space>}
                  description={<Space size="middle" wrap>{stateTag(task.state)}<Text type="secondary">{task.workstream}</Text><Text type="secondary">{task.assignee ?? "未派单"}</Text></Space>} />
              </List.Item>
            )} />
          </Card>
        </Col>
        <Col xs={24} lg={9}>
          <Card title="Agent 状态" size="small" extra={<Button type="link" onClick={() => setPage("agents")}>全部</Button>}>
            <List dataSource={agents.slice(0, 7)} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Agent" /> }} renderItem={(agent) => (
              <List.Item>
                <List.Item.Meta avatar={<Badge status={AGENT_BADGE[agent.status] ?? "default"} offset={[-2, 18]}><Avatar icon={<UserOutlined />} style={{ background: "#f2f4f7", color: "#344054" }} /></Badge>}
                  title={<Text strong>{agent.code}</Text>}
                  description={<Text type="secondary">{AGENT_LABEL[agent.status] ?? agent.status}{agent.current_task ? ` · ${agent.current_task}` : " · 空闲"}</Text>} />
              </List.Item>
            )} />
          </Card>
        </Col>
      </Row>

      <Card title="实时协作流" size="small" className="section" extra={<Text type="secondary">最近 {keyEvents.length} 条关键事件</Text>}>
        <List dataSource={keyEvents.slice(0, 8)} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待事件" /> }} renderItem={(event) => {
          const label = eventLabel(event);
          return (
            <List.Item>
              <div className="event-row">
                <Tag color={event.type.startsWith("tool") ? "geekblue" : event.type.startsWith("merge") ? "green" : "default"}>{label.title}</Tag>
                {event.task && <Text strong>{event.task}</Text>}
                <Text type="secondary" ellipsis>{label.detail}</Text>
                <span className="event-time">{time(event.occurred_at)}</span>
              </div>
            </List.Item>
          );
        }} />
      </Card>
    </>
  );

  const tasksPage = () => (
    <Card title="任务列表" className="table-card" extra={token ? <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建任务</Button> : undefined}>
      <Space wrap className="toolbar">
        <Input.Search allowClear placeholder="搜索编号、标题、Agent" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 280 }} />
        <Select value={stateFilter} onChange={setStateFilter} style={{ width: 150 }} options={[{ value: "all", label: "全部状态" }, ...STATE_ORDER.map((s) => ({ value: s, label: LABELS[s] }))]} />
        <Text type="secondary">共 {filteredTasks.length} 个</Text>
        {selectedTaskCodes.length > 0 && (
          <Space className="batch-bar" wrap>
            <Text strong>已选 {selectedTaskCodes.length} 个</Text>
            <Button size="small" loading={busy} onClick={() => void batchDispatch()}>批量派单</Button>
            <Button size="small" loading={busy} onClick={() => void batchRequeue()}>批量重新排队</Button>
            <Button size="small" loading={busy} onClick={() => void batchMerge()}>批量加入合并</Button>
            <Button size="small" type="link" onClick={() => setSelectedTaskCodes([])}>清除</Button>
          </Space>
        )}
      </Space>
      <Table rowKey="code" size="middle" loading={loading} columns={tasksColumns} dataSource={filteredTasks}
        pagination={{ pageSize: 12, showSizeChanger: false, showTotal: (total) => `共 ${total} 个` }}
        rowSelection={token ? { selectedRowKeys: selectedTaskCodes, onChange: (keys) => setSelectedTaskCodes(keys as string[]) } : undefined}
        onRow={(row) => ({ onClick: () => void openTask(row.code), className: "clickable" })}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的任务" /> }} scroll={{ x: 940 }} />
    </Card>
  );

  const agentsPage = () => (
    <>
      <Row gutter={[12, 12]}>
        {[["Agent 总数", agents.length], ["工作中", agentCount(agents, "working")], ["空闲", agentCount(agents, "idle")], ["等待人工", agentCount(agents, "waiting_user")]].map(([label, value]) => (
          <Col xs={12} lg={6} key={String(label)}><Card className="mini-stat"><Text type="secondary">{label}</Text><strong>{value}</strong></Card></Col>
        ))}
      </Row>
      <Row gutter={[12, 12]} className="section">
        {agents.map((agent) => (
          <Col xs={24} sm={12} xl={8} key={agent.code}>
            <Card size="small" className="agent-card">
              <div className="agent-head">
                <Avatar size={42} style={{ background: "#eef2ff", color: "#3730a3" }}>{agent.code.slice(-1).toUpperCase()}</Avatar>
                <div><Text strong>{agent.code}</Text><div><Text type="secondary">{agent.role}</Text></div></div>
                <Badge status={AGENT_BADGE[agent.status] ?? "default"} text={AGENT_LABEL[agent.status] ?? agent.status} />
              </div>
              <Divider style={{ margin: "10px 0" }} />
              <Descriptions column={1} size="small" colon={false}>
                <Descriptions.Item label="当前任务">{agent.current_task ?? "空闲"}</Descriptions.Item>
                <Descriptions.Item label="心跳">{agent.heartbeat_at ? datetime(agent.heartbeat_at) : "—"}</Descriptions.Item>
              </Descriptions>
            </Card>
          </Col>
        ))}
      </Row>
    </>
  );

  const eventsPage = () => (
    <Card title="协作事件" className="table-card" extra={<Select value={eventFilter} onChange={setEventFilter} style={{ width: 240 }} showSearch options={[{ value: "key", label: "关键事件" }, { value: "all", label: "全部事件（含工具/模型）" }, ...Array.from(new Set(events.map((e) => e.type))).map((t) => ({ value: t, label: t }))]} />}>
      <List dataSource={filteredEvents} pagination={{ pageSize: 15, showSizeChanger: false }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事件" /> }} renderItem={(event) => {
        const label = eventLabel(event);
        return (
          <List.Item>
            <div className="event-row">
              <Tag color={event.type.startsWith("tool") ? "geekblue" : event.type.startsWith("merge") ? "green" : "default"}>{label.title}</Tag>
              {event.task && <Text strong>{event.task}</Text>}
              <Text type="secondary" ellipsis style={{ maxWidth: 520 }}>{label.detail}</Text>
              <span className="event-time">{datetime(event.occurred_at)}</span>
            </div>
          </List.Item>
        );
      }} />
    </Card>
  );

  const adminPage = (key: string) => {
    if (key === "approvals") return (
      <Card title="待审批" className="table-card" extra={<Text type="secondary">L3 变更必须人工批准后才能合并</Text>}>
        <Table rowKey="id" size="middle" dataSource={approvals} columns={approvalColumns} pagination={false} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有待审批事项" /> }} />
      </Card>
    );
    if (key === "leases") return (
      <Card title="活动租约" className="table-card" extra={<Text type="secondary">热文件 single_writer</Text>}>
        <Table rowKey="id" size="middle" dataSource={leases} columns={leaseColumns} pagination={false} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有活动租约" /> }} />
      </Card>
    );
    if (key === "alerts") return (
      <Card title="未处理告警" className="table-card" extra={<Text type="secondary">Observer 每 30 秒评估</Text>}>
        <Table rowKey="id" size="middle" dataSource={alerts} columns={alertColumns} pagination={false} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="运行正常，没有未处理告警" /> }} />
      </Card>
    );
    if (key === "llm") return (
      <Card title="LLM 配置" className="table-card" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => {
        setEditingLlm(null); llmForm.resetFields();
        llmForm.setFieldsValue({ agent_code: "worker-deepseek", base_url: "https://api.deepseek.com/v1", model: "deepseek-flash", price_input_per_1m: 2, price_output_per_1m: 8, price_cache_hit_per_1m: 0.04 });
        setLlmOpen(true);
      }}>添加配置</Button>}>
        <Alert type="info" showIcon message="API key 加密存储且不回显；修改后 worker 下个任务自动生效，无需重启。" style={{ marginBottom: 14 }} />
        <Table rowKey="id" size="middle" dataSource={llmProviders} columns={llmColumns} pagination={false} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有 LLM 配置" /> }} scroll={{ x: 900 }} />
      </Card>
    );
    return (
      <Row gutter={[14, 14]}>
        <Col xs={24} lg={12}><Card title="服务入口" className="info-card">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="主入口">https://macp.example.com:9443/</Descriptions.Item>
            <Descriptions.Item label="只读入口">http://macp.example.com:8088/</Descriptions.Item>
            <Descriptions.Item label="OIDC">https://macp.example.com:9443/oidc</Descriptions.Item>
            <Descriptions.Item label="健康检查">/v1/health</Descriptions.Item>
            <Descriptions.Item label="OpenAPI">/openapi.json</Descriptions.Item>
          </Descriptions>
          <Space><Button href="/docs" target="_blank" icon={<FileTextOutlined />}>API 文档</Button><Button href="/openapi.json" target="_blank">OpenAPI</Button></Space>
        </Card></Col>
        <Col xs={24} lg={12}><Card title="当前状态" className="info-card">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="项目">{project}</Descriptions.Item>
            <Descriptions.Item label="实时连接">{connected ? "已连接" : "断开"}</Descriptions.Item>
            <Descriptions.Item label="最近事件">{datetime(lastEventAt ?? overview?.last_event_at)}</Descriptions.Item>
            <Descriptions.Item label="模型成本">¥{(overview?.model_cost_today ?? 0).toFixed(4)}</Descriptions.Item>
          </Descriptions>
        </Card></Col>
      </Row>
    );
  };

  const pageTitle: Record<string, [string, string]> = {
    overview: ["总览", "任务、Agent 与实时事件的整体状态"],
    tasks: ["任务", "创建、派单、验收与合并"],
    agents: ["Agent", "在线状态、当前任务与心跳"],
    events: ["协作事件", "实时审计流，按类型筛选"],
    approvals: ["审批", "敏感操作等待人工决定"],
    leases: ["租约", "热文件与工作区独占锁"],
    alerts: ["告警", "Observer 产生的运行异常"],
    llm: ["LLM 配置", "地址、模型与密钥的加密配置"],
    api: ["API 与版本", "服务入口、健康检查与 OpenAPI"],
    projects: ["项目管理", "新建、改名与切换项目"],
    artifacts: ["产物与证据", "构建溯源、标签包与门禁日志下载"],
    chain: ["上链情况", "本地记录与联盟链账本的对照"],
    "agents-admin": ["Agent 管理", "角色、状态、能力与允许路径"],
    workload: ["分工看板", "每个 Agent 在做什么、负载与在办任务"],
    models: ["模型指派", "为 Agent 选择模型来源与模型名"],
  };
  const NAV: Array<{ key: string; label: string; icon: React.ReactNode; pages: Array<[string, string]> }> = [
    { key: "overview", label: "总览", icon: <DashboardOutlined />, pages: [["overview", "总览"]] },
    { key: "guide", label: "指南", icon: <ReadOutlined />, pages: [["guide", "使用指引"]] },
    { key: "tasks", label: `任务 ${tasks.length}`, icon: <UnorderedListOutlined />, pages: [["tasks", "任务"]] },
    { key: "agent-hub", label: "Agent", icon: <TeamOutlined />, pages: [
      ["agents", "列表"], ["workload", "分工"], ["models", "模型"], ["agents-admin", "配置"],
    ] },
    { key: "artifacts", label: "产物", icon: <InboxOutlined />, pages: [["artifacts", "产物与证据"]] },
    { key: "audit", label: "审计", icon: <ClusterOutlined />, pages: [
      ["events", "事件流"], ["chain", "上链"],
    ] },
    { key: "ops", label: "运营", icon: <SafetyCertificateOutlined />, pages: [
      ["approvals", "审批"], ["leases", "租约"], ["alerts", "告警"],
    ] },
    { key: "settings", label: "设置", icon: <ApiOutlined />, pages: [
      ["projects", "项目"], ["llm", "LLM 供应商"], ["api", "API 与版本"],
    ] },
  ];
  const activeGroup = NAV.find((group) => group.pages.some(([key]) => key === page)) ?? NAV[0];
  const NAV_GROUP = (key: string) => {
    const group = NAV.find((item) => item.key === key);
    return group ? group.pages[0][0] : "overview";
  };

  const renderPage = (key: string) => {
    if (key === "overview") return overviewPage();
    if (key === "tasks") return tasksPage();
    if (key === "agents") return agentsPage();
    if (key === "events") return eventsPage();
    if (adminPageKeys.includes(key as UiPageKey)) return <AdminPage page={key as UiPageKey} onNavigate={setPage} />;
    return adminPage(key);
  };

  const content = () => {
    if (activeGroup.pages.length === 1) return renderPage(activeGroup.pages[0][0]);
    return (
      <Tabs
        activeKey={page}
        onChange={setPage}
        items={activeGroup.pages.map(([key, label]) => ({ key, label, children: renderPage(key) }))}
      />
    );
  };

  return (
    <Layout className="shell">
      <Sider width={216} collapsible collapsed={collapsed} trigger={null} breakpoint="lg" collapsedWidth={0} className="sider">
        <div className="brand"><div className="brand-mark">M</div><div><strong>MACP</strong><span>Mission Control</span></div></div>
        <div className="project-picker">
          <Text type="secondary">当前项目</Text>
          <Select value={project} style={{ width: "100%", marginTop: 6 }}
            options={[{ value: "demo", label: "demo" }, ...projects.filter((p) => p.code !== "demo").map((p) => ({ value: p.code, label: `${p.code} · ${p.name}` }))]}
            onChange={(value) => { setProject(value); setProjectState(value); window.location.reload(); }} />
        </div>
        <Menu mode="inline" selectedKeys={[activeGroup.key]} onClick={({ key }) => setPage(NAV_GROUP(key))} className="nav" items={NAV.map((group) => ({
          key: group.key,
          icon: group.icon,
          label: group.label,
        }))} />

        <div className="sider-foot">
          <div><Badge status={connected ? "processing" : "error"} text={connected ? "实时连接" : "连接中断"} /><div><Text type="secondary">{token ? "管理员模式" : "只读访客"}</Text></div></div>
          <Text type="secondary">v0.2.0</Text>
        </div>
      </Sider>
      <Layout>
        <Header className="topbar">
          <Space size={12}>
            <Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed((v) => !v)} />
            <div>
                <Title level={4}>{(pageTitle[page] ?? ["未命名页面", ""])[0]}</Title>
                <Text type="secondary">{(pageTitle[page] ?? ["", ""])[1]}</Text>
              </div>
          </Space>
          <Space wrap>
            {token ? (
              <>
                <Tag color="green" style={{ marginInlineEnd: 2 }}>
                  已登录 · {authMethod === "sso" ? (decodeIdentity(token) || "SSO 管理员") : "管理员 Token"}
                </Tag>
                <Button icon={<LogoutOutlined />} onClick={() => {
                  setToken(""); setTokenState(""); setAuthMethodState("");
                  window.localStorage.removeItem("macp_auth_method");
                  setPage("overview");
                  void load(); void refreshAdmin();
                  message.info("已退出登录");
                }}>退出登录</Button>
              </>
            ) : (
              <>
                <Tag color="gold" style={{ marginInlineEnd: 2 }}>只读访客</Tag>
                <Button icon={<LoginOutlined />} onClick={() => void loginWithSso()}>SSO 登录</Button>
                <Button icon={<LockOutlined />} onClick={() => setTokenOpen(true)}>配置 Token</Button>
              </>
            )}
            {token && <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建任务</Button>}
            <Button icon={<ReloadOutlined />} onClick={() => void load()} />
          </Space>
        </Header>
        <Content className="content">
          {error && <Alert type="error" showIcon message="接口请求失败" description={error} style={{ marginBottom: 12 }} />}
          {!secure && <Alert type="warning" showIcon message={`当前是只读 HTTP。写操作请使用 https://${window.location.hostname}:9443（需信任内部 CA）。`} style={{ marginBottom: 12 }} />}
          {secure && !token && <Alert type="info" showIcon message="当前未登录，只能查看。配置 LLM、审批、租约和告警需要 SSO 登录或管理员 Token；任务写操作在右上角。" style={{ marginBottom: 12 }} />}
          {content()}
        </Content>
      </Layout>

      <Drawer title={selected ? `${selected.code} · ${selected.title}` : "任务详情"} width={680} open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        {selected && (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {actionPanel()}
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="状态">{stateTag(selected.state)}</Descriptions.Item>
              <Descriptions.Item label="工作流">{selected.workstream}</Descriptions.Item>
              <Descriptions.Item label="执行者">{selected.assignee ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="优先级">{priorityTag(selected.priority)}</Descriptions.Item>
              <Descriptions.Item label="类型">{selected.kind}</Descriptions.Item>
              <Descriptions.Item label="尝试">{selected.current_attempt}</Descriptions.Item>
              <Descriptions.Item label="更新时间" span={2}>{datetime(selected.updated_at)}</Descriptions.Item>
            </Descriptions>
            {selected.documents?.length > 0 && (
              <Card size="small" title="需求文档"><List size="small" dataSource={selected.documents} renderItem={(doc) => (
                <List.Item actions={[<Button size="small" key="v" onClick={() => void showDoc(doc)}>查看正文</Button>]}>
                  <Space><Tag color="blue">{doc.filename}</Tag><Text type="secondary">{doc.char_count} 字符</Text></Space>
                </List.Item>
              )} /></Card>
            )}
            {selected.receipt && (
              <Card size="small" title="最新回执"><Descriptions column={1} size="small">
                <Descriptions.Item label="编号">{selected.receipt.code}</Descriptions.Item>
                <Descriptions.Item label="状态">{selected.receipt.status}</Descriptions.Item>
                <Descriptions.Item label="风险">{String(selected.receipt.body.risk_level ?? "L1")}</Descriptions.Item>
                <Descriptions.Item label="改动文件">{(selected.receipt.body.changed_paths as string[] | undefined)?.join(", ") ?? "—"}</Descriptions.Item>
                <Descriptions.Item label="Stale base">{selected.receipt.stale_base ? <Tag color="red">是</Tag> : "否"}</Descriptions.Item>
              </Descriptions></Card>
            )}
            {selected.leases.length > 0 && (
              <Card size="small" title="活动租约"><List size="small" dataSource={selected.leases} renderItem={(lease) => (
                <List.Item><Space><Tag>{lease.path_glob}</Tag><Text type="secondary">到期 {datetime(lease.expires_at)}</Text></Space></List.Item>
              )} /></Card>
            )}
            <Card size="small" title="事件时间线"><Timeline items={selected.events.map((event) => {
              const label = eventLabel(event);
              return {
                color: event.type.includes("fail") || event.type.includes("block") ? "red" : event.type.includes("accepted") || event.type.includes("merged") ? "green" : "blue",
                children: <div><Space wrap><Text strong>{label.title}</Text><Text type="secondary">{datetime(event.occurred_at)}</Text></Space>{label.detail && <div><Text type="secondary">{label.detail}</Text></div>}</div>,
              };
            })} /></Card>
          </Space>
        )}
      </Drawer>

      <Modal title="管理员 Token" open={tokenOpen} onCancel={() => setTokenOpen(false)} onOk={async () => {
        const values = await tokenForm.validateFields();
        setToken(values.token.trim()); setTokenState(values.token.trim());
        window.localStorage.setItem("macp_auth_method", "token"); setAuthMethodState("token");
        setTokenOpen(false); message.success("Token 已保存到本地浏览器"); await refreshAdmin();
      }} okText="保存">
        <Alert type="warning" showIcon message="Token 只保存在本机 localStorage；生产请通过 HTTPS 访问。" style={{ marginBottom: 12 }} />
        <Form form={tokenForm} layout="vertical" initialValues={{ token }}>
          <Form.Item name="token" label="Bearer Token" rules={[{ required: true }]}><Input.Password placeholder="MACP write token" /></Form.Item>
          <Button danger onClick={() => { setToken(""); setTokenState(""); window.localStorage.removeItem("macp_auth_method"); setAuthMethodState(""); tokenForm.resetFields(); }}>清除 Token</Button>
        </Form>
      </Modal>

      <Modal title="新建任务" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => void createTask()} confirmLoading={busy} okText="创建并生成 Work Order" width={640}>
        <Form form={taskForm} layout="vertical" initialValues={{ workstream: "stream-core", priority: "P2", kind: "slice", max_cost: 2 }}>
          <Form.Item label="需求文档（可选）">
            <Upload.Dragger multiple showUploadList={false} beforeUpload={(file) => { void uploadDoc(file); return false; }} disabled={uploading}>
              <p className="ant-upload-drag-icon"><UploadOutlined /></p>
              <p className="ant-upload-text">点击或拖入需求文档</p>
              <p className="ant-upload-hint">md / txt / pdf / docx / doc，单文件最大 10MB；正文随任务保存，Agent 可分段读取。</p>
            </Upload.Dragger>
            {uploadedDocs.length > 0 && <Space wrap style={{ marginTop: 8 }}>{uploadedDocs.map((doc) => (
              <Tag key={doc.id} color="blue" closable onClose={() => setUploadedDocs((prev) => prev.filter((x) => x.id !== doc.id))}>{doc.filename} · {doc.char_count} 字</Tag>
            ))}</Space>}
          </Form.Item>
          <Row gutter={12}>
            <Col span={8}><Form.Item name="code" label="任务编号" rules={[{ required: true, pattern: /^T-\d{4,}$/ }]}><Input placeholder="T-900001" /></Form.Item></Col>
            <Col span={16}><Form.Item name="title" label="标题" rules={[{ required: true }]}><Input placeholder="一句话说明要做什么" /></Form.Item></Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}><Form.Item name="workstream" label="工作流"><Select options={[{ value: "stream-core", label: "Core" }, { value: "stream-ui", label: "UI" }, { value: "stream-infra", label: "Infra" }]} /></Form.Item></Col>
            <Col span={8}><Form.Item name="priority" label="优先级"><Select options={["P0", "P1", "P2", "P3"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
            <Col span={8}><Form.Item name="kind" label="类型"><Select options={["slice", "fix", "infra", "migration"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
          </Row>
          <Form.Item name="max_cost" label="成本上限（CNY）"><Input type="number" min={0} step={0.1} /></Form.Item>
          <Text type="secondary">默认写面：solution.py / src/** / tests/** / README.md；锚点按 main 解析。</Text>
        </Form>
      </Modal>

      <Modal title={editingLlm ? `编辑 LLM · ${editingLlm.agent_code}` : "添加 LLM 配置"} open={llmOpen}
        onCancel={() => { setLlmOpen(false); setEditingLlm(null); }} onOk={() => void saveLlm()} confirmLoading={busy} okText="保存" width={620}>
        <Alert type="warning" showIcon message="API key 加密落库、不回显；留空保持原 key。base_url 必须是 OpenAI 兼容的 /v1 地址。" style={{ marginBottom: 12 }} />
        <Form form={llmForm} layout="vertical">
          <Form.Item name="agent_code" label="Agent" rules={[{ required: true }]}><Select options={agents.filter((a) => a.role === "worker").map((a) => ({ value: a.code, label: a.code }))} /></Form.Item>
          <Form.Item name="name" label="名称"><Input placeholder="DeepSeek Flash" /></Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}><Input placeholder="https://api.deepseek.com/v1" /></Form.Item>
          <Form.Item name="model" label="Model" rules={[{ required: true }]}><Input placeholder="deepseek-flash" /></Form.Item>
          <Form.Item name="api_key" label="API Key"><Input.Password placeholder={editingLlm?.api_key_set ? "留空保持原 key" : "sk-..."} autoComplete="new-password" /></Form.Item>
          <Row gutter={12}>
            <Col span={8}><Form.Item name="price_input_per_1m" label="输入价/百万"><Input type="number" step={0.01} /></Form.Item></Col>
            <Col span={8}><Form.Item name="price_output_per_1m" label="输出价/百万"><Input type="number" step={0.01} /></Form.Item></Col>
            <Col span={8}><Form.Item name="price_cache_hit_per_1m" label="缓存命中价/百万"><Input type="number" step={0.01} /></Form.Item></Col>
          </Row>
          <Text type="secondary">只有 LLM_CONFIG_FROM_API=1 的 worker 会读取数据库配置；当前 worker-deepseek 已启用。</Text>
        </Form>
      </Modal>

      <Modal title={`需求文档 · ${docName}`} open={docOpen} onCancel={() => setDocOpen(false)} footer={null} width={820}>
        <Input.TextArea value={docText} readOnly autoSize={{ minRows: 18, maxRows: 30 }} style={{ fontSize: 12, fontFamily: "ui-monospace, Menlo, monospace" }} />
      </Modal>
    </Layout>
  );
}