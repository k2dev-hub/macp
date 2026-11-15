import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Layout,
  List,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Upload,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  AlertOutlined,
  BranchesOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  LockOutlined,
  LoginOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { EChartsOption } from "echarts";
import { api, getProject, getToken, setProject, setToken } from "./api";
import EChart from "./EChart";
import type {
  Agent,
  AlertRow,
  Approval,
  EventItem,
  DocumentMeta,
  Lease,
  LlmProvider,
  Metrics,
  Overview,
  Task,
  TaskDetail,
  TaskState,
} from "./types";

const { Header, Content } = Layout;
const { Title, Text } = Typography;
const { TextArea } = Input;

const STATE_LABELS: Record<string, string> = {
  ready: "待派单",
  assigned: "已派单",
  in_progress: "执行中",
  blocked: "阻塞",
  review: "待验收",
  accepted: "已验收",
  done: "完成",
  reopened: "重开",
  cancelled: "取消",
  failed: "失败",
  draft: "草稿",
  merge_queued: "待合并",
  merging: "合并中",
  merged: "已合并",
};

const STATE_COLORS: Record<string, string> = {
  ready: "default",
  assigned: "blue",
  in_progress: "geekblue",
  blocked: "red",
  review: "gold",
  accepted: "green",
  done: "green",
  reopened: "orange",
  cancelled: "default",
  failed: "red",
};

const STATE_ORDER = ["ready", "assigned", "in_progress", "blocked", "review", "accepted"];

const STATE_BAR_COLORS: Record<string, string> = {
  ready: "#98a2b3",
  assigned: "#1677ff",
  in_progress: "#2f54eb",
  blocked: "#f04438",
  review: "#f79009",
  accepted: "#12b76a",
};

const AGENT_STATUS: Record<string, "success" | "processing" | "warning" | "error" | "default"> = {
  idle: "success",
  working: "processing",
  waiting_user: "warning",
  blocked: "error",
  offline: "default",
  stalled: "error",
  dead: "error",
};

const EVENT_TYPES = [
  "agent.registered",
  "agent.status_changed",
  "agent.heartbeat_missed",
  "task.created",
  "task.validated",
  "task.assigned",
  "task.started",
  "task.blocked",
  "task.resumed",
  "task.completed",
  "task.rejected",
  "task.accepted",
  "task.done",
  "task.merged",
  "task.reopened",
  "task.cancelled",
  "task.failed",
  "task.requeued",
  "assignment.offered",
  "assignment.claimed",
  "assignment.submitted",
  "assignment.expired",
  "assignment.revoked",
  "assignment.closed",
  "attempt.created",
  "lease.granted",
  "lease.renewed",
  "lease.expired",
  "lease.released",
  "lease.conflict",
  "gate.started",
  "gate.passed",
  "gate.failed",
  "review.accepted",
  "review.rejected",
  "merge.queued",
  "merge.verifying",
  "merge.conflict",
  "merge.merged",
  "merge.requeued",
  "merge.approval_required",
  "push.done",
  "merge.reverted",
  "merge.cancelled",
  "merge.failed",
  "approval.requested",
  "approval.approved",
  "approval.denied",
  "budget.exhausted",
  "alert.opened",
  "alert.cleared",
  "observer.scanned",
  "tool.called",
  "tool.succeeded",
  "tool.failed",
  "model.usage",
];

function fmtTime(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function stateLabel(state: string) {
  return STATE_LABELS[state] ?? state;
}

function useAdminData(enabled: boolean) {
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [leases, setLeases] = useState<Lease[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [llmProviders, setLlmProviders] = useState<LlmProvider[]>([]);

  const refreshAdmin = useCallback(async () => {
    if (!enabled) {
      setAlerts([]);
      setLeases([]);
      setApprovals([]);
      setLlmProviders([]);
      return;
    }
    try {
      const [a, l, p, llm] = await Promise.all([
        api.alerts(),
        api.leases(),
        api.approvals(),
        api.llmProviders(),
      ]);
      setAlerts(a);
      setLeases(l);
      setApprovals(p);
      setLlmProviders(llm);
    } catch {
      // Keep the read-only dashboard usable when the token is wrong.
    }
  }, [enabled]);

  return { alerts, leases, approvals, llmProviders, refreshAdmin };
}

export default function App() {
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
  const [token, setTokenState] = useState(getToken());
  const [project, setProjectState] = useState(getProject());
  const [projects, setProjects] = useState<Array<{ code: string; name: string }>>([]);
  const [tokenOpen, setTokenOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mergeIds, setMergeIds] = useState<Record<string, string>>({});
  const [llmOpen, setLlmOpen] = useState(false);
  const [editingLlm, setEditingLlm] = useState<LlmProvider | null>(null);
  const [llmForm] = Form.useForm();
  const [uploadedDocs, setUploadedDocs] = useState<DocumentMeta[]>([]);
  const [uploading, setUploading] = useState(false);
  const [docOpen, setDocOpen] = useState(false);
  const [docText, setDocText] = useState("");
  const [docName, setDocName] = useState("");
  const [rejectFindings, setRejectFindings] = useState("");
  const [taskForm] = Form.useForm();
  const [tokenForm] = Form.useForm();
  const refreshTimer = useRef<number | null>(null);
  const { alerts, leases, approvals, llmProviders, refreshAdmin } = useAdminData(Boolean(token));

  const load = useCallback(async () => {
    try {
      const [o, t, a, m] = await Promise.all([
        api.overview(),
        api.tasks(),
        api.agents(),
        api.metrics(),
      ]);
      setOverview(o);
      setTasks(t);
      setAgents(a);
      setMetrics(m);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const loginWithSso = useCallback(async () => {
    try {
      const config = await api.authConfig();
      const state = Math.random().toString(36).slice(2);
      window.localStorage.setItem("macp_oidc_state", state);
      const secureOrigin =
        window.location.protocol === "https:"
          ? window.location.origin
          : `https://${window.location.hostname}:9443`;
      const params = new URLSearchParams({
        client_id: config.client_id,
        redirect_uri: secureOrigin + "/",
        response_type: "code",
        scope: config.scope,
        state,
      });
      window.location.href = `${config.issuer}/authorize?${params.toString()}`;
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code) return;
    const expected = window.localStorage.getItem("macp_oidc_state");
    if (!expected || params.get("state") !== expected) {
      message.error("SSO state 校验失败，请重新登录");
      return;
    }
    void (async () => {
      try {
        const result = await api.exchangeCode(code, window.location.origin + "/");
        setToken(result.access_token);
        setTokenState(result.access_token);
        window.localStorage.removeItem("macp_oidc_state");
        window.history.replaceState({}, "", window.location.pathname);
        message.success("SSO 登录成功");
        await load();
        await refreshAdmin();
      } catch (err) {
        message.error(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [load, refreshAdmin]);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) {
      window.clearTimeout(refreshTimer.current);
    }
    refreshTimer.current = window.setTimeout(() => {
      void load();
      void refreshAdmin();
    }, 250);
  }, [load, refreshAdmin]);

  useEffect(() => {
    void load();
    void refreshAdmin();
    const timer = window.setInterval(() => {
      void load();
      void refreshAdmin();
    }, 5000);
    return () => {
      window.clearInterval(timer);
      if (refreshTimer.current) {
        window.clearTimeout(refreshTimer.current);
      }
    };
  }, [load, refreshAdmin]);

  useEffect(() => {
    void refreshAdmin();
    if (token) {
      void api
        .projects()
        .then((rows) => setProjects(rows))
        .catch(() => setProjects([]));
    }
  }, [token, refreshAdmin]);

  useEffect(() => {
    const initial = async () => {
      try {
        const items = await api.events(0);
        setEvents((prev) => {
          const merged = new Map<number, EventItem>();
          for (const item of [...items, ...prev]) {
            merged.set(item.seq, item);
          }
          return Array.from(merged.values())
            .sort((a, b) => b.seq - a.seq)
            .slice(0, 80);
        });
      } catch {
        // The stream will fill the feed.
      }
    };
    void initial();

    const es = new EventSource(`/v1/events/stream?since=0&project=${getProject()}`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    const handle = (messageEvent: MessageEvent) => {
      try {
        const event = JSON.parse(messageEvent.data) as EventItem;
        setLastEventAt(event.occurred_at);
        setEvents((prev) => {
          if (prev.some((item) => item.seq === event.seq)) return prev;
          return [event, ...prev].slice(0, 80);
        });
        if (!event.type.startsWith("agent.")) {
          scheduleRefresh();
        }
      } catch {
        // Ignore malformed event.
      }
    };
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, handle as EventListener);
    }
    return () => es.close();
  }, [scheduleRefresh]);

  const openTask = async (code: string) => {
    try {
      const detail = await api.task(code);
      setSelected(detail);
      setRejectFindings("");
      setDrawerOpen(true);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      message.success(`${label}成功`);
      await load();
      await refreshAdmin();
      if (selected) {
        setSelected(await api.task(selected.code));
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const createTask = async () => {
    const values = await taskForm.validateFields();
    await act("创建任务", async () => {
      await api.createTask({
        code: values.code,
        title: values.title,
        workstream: values.workstream,
        priority: values.priority,
        kind: values.kind,
        documents: uploadedDocs.map((doc) => doc.id),
        budget: {
          max_tool_calls: 40,
          max_model_calls: 30,
          max_tokens: 400000,
          max_cost: values.max_cost ?? 2,
          max_wall_seconds: 900,
        },
      });
      setCreateOpen(false);
      taskForm.resetFields();
      setUploadedDocs([]);
    });
  };

  const uploadDoc = async (file: File) => {
    setUploading(true);
    try {
      const uploaded = await api.uploadDocument(file);
      setUploadedDocs((prev) => [...prev, uploaded]);
      if (uploaded.warnings?.length) {
        message.warning(`${uploaded.filename}: ${uploaded.warnings.join("; ")}`);
      } else {
        message.success(`${uploaded.filename} 已解析 ${uploaded.char_count} 字符`);
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
    }
    return false;
  };

  const showDocument = async (doc: DocumentMeta) => {
    try {
      const result = await api.documentText(doc.id);
      setDocName(doc.filename);
      setDocText(result.text);
      setDocOpen(true);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const dispatchTask = async () => {
    if (!selected) return;
    const agent = (selected as unknown as { _agent?: string })._agent;
    if (!agent) {
      message.warning("请选择空闲 Agent");
      return;
    }
    await act("派单", () => api.dispatch(selected.code, agent));
  };

  const reviewTask = async (decision: "accept" | "reject") => {
    if (!selected?.receipt) {
      message.warning("该任务没有 Receipt");
      return;
    }
    if (decision === "reject" && !rejectFindings.trim()) {
      message.warning("驳回必须填写 findings");
      return;
    }
    await act(decision === "accept" ? "验收" : "驳回", () =>
      api.review(selected.code, {
        receipt_id: selected.receipt?.code,
        decision,
        findings: decision === "reject" ? [rejectFindings.trim()] : [],
        reviewer: "coordinator-1",
      }),
    );
  };

  const enqueueMerge = async () => {
    if (!selected) return;
    await act("加入合并队列", async () => {
      const result = await api.enqueueMerge(selected.code);
      setMergeIds((prev) => ({ ...prev, [selected.code]: result.merge_id }));
      message.info(`merge ${result.merge_id.slice(0, 8)} 已排队`);
    });
  };

  const processMerge = async () => {
    if (!selected) return;
    const mergeId = mergeIds[selected.code];
    if (!mergeId) {
      message.warning("先加入合并队列");
      return;
    }
    await act("合并", () => api.processMerge(mergeId));
  };

  const requestMergeApproval = async () => {
    if (!selected) return;
    await act("请求审批", async () => {
      await api.requestApproval({
        task: selected.code,
        action: "merge",
        risk: "high",
        options: ["approved", "denied"],
        default_option: "denied",
        ttl_hours: 24,
      });
    });
  };

  const stateOptions = useMemo<EChartsOption>(() => {
    const counts = new Map((metrics?.states ?? []).map((s) => [s.state, s.count]));
    const states = STATE_ORDER.map((state) => ({
      state,
      count: counts.get(state) ?? 0,
    }));
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 36, right: 16, top: 24, bottom: 28 },
      xAxis: {
        type: "category",
        data: states.map((s) => stateLabel(s.state)),
        axisLabel: { color: "#667085" },
      },
      yAxis: { type: "value", minInterval: 1, axisLabel: { color: "#667085" } },
      series: [
        {
          type: "bar",
          data: states.map((s) => ({
            value: s.count,
            itemStyle: {
              color: STATE_BAR_COLORS[s.state] ?? "#1677ff",
              borderRadius: [6, 6, 0, 0],
            },
          })),
          barMaxWidth: 36,
        },
      ],
    };
  }, [metrics]);

  const eventRateOption = useMemo<EChartsOption>(() => {
    const rate = metrics?.event_rate ?? [];
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 36, right: 16, top: 24, bottom: 28 },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: rate.map((r) => r.minute),
        axisLabel: { color: "#667085" },
      },
      yAxis: { type: "value", minInterval: 1, axisLabel: { color: "#667085" } },
      series: [
        {
          type: "line",
          smooth: true,
          data: rate.map((r) => r.count),
          symbol: "circle",
          symbolSize: 5,
          lineStyle: { color: "#13c2c2", width: 2 },
          itemStyle: { color: "#13c2c2" },
          areaStyle: { color: "rgba(19,194,194,0.12)" },
        },
      ],
    };
  }, [metrics]);

  const workstreamOption = useMemo<EChartsOption>(() => {
    const rows = metrics?.workstreams ?? [];
    const max = Math.max(1, ...rows.map((r) => r.wip_limit));
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 70, right: 18, top: 20, bottom: 24 },
      xAxis: {
        type: "value",
        min: 0,
        max,
        minInterval: 1,
        axisLabel: { color: "#667085" },
      },
      yAxis: {
        type: "category",
        data: rows.map((r) => r.name),
        axisLabel: { color: "#667085" },
      },
      series: [
        {
          name: "容量",
          type: "bar",
          data: rows.map((r) => r.wip_limit),
          barMaxWidth: 22,
          barGap: "-100%",
          itemStyle: { color: "#e9edf5", borderRadius: [0, 6, 6, 0] },
          silent: true,
        },
        {
          name: "当前 WIP",
          type: "bar",
          data: rows.map((r) => r.wip),
          barMaxWidth: 22,
          itemStyle: { color: "#722ed1", borderRadius: [0, 6, 6, 0] },
        },
      ],
    };
  }, [metrics]);

  const idleAgents = agents.filter((a) => a.status === "idle" && a.role === "worker");

  const columns: ColumnsType<Task> = [
    {
      title: "任务",
      dataIndex: "code",
      width: 100,
      render: (code: string) => (
        <Button
          type="link"
          style={{ padding: 0 }}
          onClick={(event) => {
            event.stopPropagation();
            void openTask(code);
          }}
        >
          {code}
        </Button>
      ),
    },
    { title: "标题", dataIndex: "title", ellipsis: true },
    {
      title: "状态",
      dataIndex: "state",
      width: 96,
      render: (state: TaskState) => <Tag color={STATE_COLORS[state]}>{stateLabel(state)}</Tag>,
    },
    { title: "Workstream", dataIndex: "workstream", width: 120 },
    {
      title: "执行者",
      dataIndex: "assignee",
      width: 100,
      render: (value: string | null) => value ?? "—",
    },
    { title: "优先级", dataIndex: "priority", width: 80 },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 100,
      render: (value: string) => fmtTime(value),
    },
  ];

  const approvalColumns: ColumnsType<Approval> = [
    { title: "任务", dataIndex: "task", width: 90 },
    { title: "动作", dataIndex: "action", width: 100 },
    {
      title: "风险",
      dataIndex: "risk",
      width: 80,
      render: (risk: string) => (
        <Tag color={risk === "critical" || risk === "high" ? "red" : "gold"}>{risk}</Tag>
      ),
    },
    { title: "申请时间", dataIndex: "requested_at", width: 110, render: fmtTime },
    {
      title: "操作",
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            type="primary"
            onClick={() =>
              void act("批准", () => api.decideApproval(record.id, "approved", "通过控制台批准"))
            }
          >
            批准
          </Button>
          <Button
            size="small"
            danger
            onClick={() =>
              void act("拒绝", () => api.decideApproval(record.id, "denied", "通过控制台拒绝"))
            }
          >
            拒绝
          </Button>
        </Space>
      ),
    },
  ];

  const leaseColumns: ColumnsType<Lease> = [
    { title: "任务", dataIndex: "task", width: 90 },
    { title: "路径", dataIndex: "path_glob" },
    { title: "Agent", dataIndex: "agent", width: 120 },
    { title: "到期", dataIndex: "expires_at", width: 110, render: fmtTime },
    {
      title: "操作",
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => void act("续租", () => api.renewLease(record.id))}>
            续租
          </Button>
          <Popconfirm title="释放该租约？" onConfirm={() => void act("释放", () => api.releaseLease(record.id))}>
            <Button size="small" danger>
              释放
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const alertColumns: ColumnsType<AlertRow> = [
    {
      title: "级别",
      dataIndex: "severity",
      width: 88,
      render: (value: string) => (
        <Tag color={value === "critical" ? "red" : value === "warning" ? "gold" : "blue"}>
          {value}
        </Tag>
      ),
    },
    { title: "规则", dataIndex: "code", width: 180 },
    { title: "次数", dataIndex: "count", width: 64 },
    { title: "最近", dataIndex: "last_seen", width: 110, render: fmtTime },
    {
      title: "详情",
      dataIndex: "details",
      ellipsis: true,
      render: (value: Record<string, unknown>) => JSON.stringify(value),
    },
    {
      title: "操作",
      width: 150,
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => void act("确认告警", () => api.ackAlert(record.id, "acked"))}>
            确认
          </Button>
          <Button size="small" onClick={() => void act("清除告警", () => api.ackAlert(record.id, "cleared"))}>
            清除
          </Button>
        </Space>
      ),
    },
  ];

  const llmColumns: ColumnsType<LlmProvider> = [
    { title: "Agent", dataIndex: "agent_code", width: 130 },
    { title: "模型", dataIndex: "model", width: 160 },
    { title: "Base URL", dataIndex: "base_url", ellipsis: true },
    {
      title: "API Key",
      dataIndex: "api_key_set",
      width: 110,
      render: (set: boolean, row) =>
        set ? <Tag color="green">已配置</Tag> : <Tag>未配置</Tag>,
    },
    {
      title: "价格/百万",
      width: 150,
      render: (_, row) => `${row.price_input_per_1m}/${row.price_output_per_1m}`,
    },
    {
      title: "状态",
      dataIndex: "enabled",
      width: 80,
      render: (enabled: boolean) => (enabled ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: "操作",
      width: 220,
      render: (_, row) => (
        <Space>
          <Button
            size="small"
            onClick={() =>
              void act("测试连接", async () => {
                const result = await api.testLlmProvider(row.id);
                if (result.ok) {
                  message.success(`连接成功，模型数 ${result.models?.length ?? 0}`);
                } else {
                  message.error(`连接失败 ${result.status}: ${result.error ?? ""}`);
                }
              })
            }
          >
            测试
          </Button>
          <Button
            size="small"
            onClick={() => {
              setEditingLlm(row);
              llmForm.setFieldsValue({ ...row, api_key: "" });
              setLlmOpen(true);
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title="停用该 LLM 配置？"
            onConfirm={() => void act("停用 LLM", () => api.disableLlmProvider(row.id))}
          >
            <Button size="small" danger>
              停用
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const saveLlm = async () => {
    const values = await llmForm.validateFields();
    await act("保存 LLM 配置", async () => {
      await api.saveLlmProvider({
        ...values,
        api_key: values.api_key?.trim() || undefined,
        price_input_per_1m: Number(values.price_input_per_1m || 0),
        price_output_per_1m: Number(values.price_output_per_1m || 0),
        price_cache_hit_per_1m: Number(values.price_cache_hit_per_1m || 0),
      });
      setLlmOpen(false);
      setEditingLlm(null);
      llmForm.resetFields();
    });
  };

  const actionPanel = () => {
    if (!selected) return null;
    const mergeId = mergeIds[selected.code];
    return (
      <Card size="small" title="操作" style={{ background: "#fafafa" }}>
        <Space wrap>
          {selected.state === "ready" && (
            <>
              <Select
                placeholder="选择空闲 Agent"
                style={{ width: 180 }}
                options={idleAgents.map((a) => ({ value: a.code, label: a.code }))}
                onChange={(value) =>
                  setSelected((prev) =>
                    prev ? ({ ...prev, _agent: value } as TaskDetail) : prev,
                  )
                }
              />
              <Button type="primary" loading={busy} onClick={() => void dispatchTask()}>
                派单
              </Button>
            </>
          )}
          {selected.state === "review" && selected.receipt && (
            <>
              <Button
                type="primary"
                loading={busy}
                onClick={() => void reviewTask("accept")}
              >
                验收通过
              </Button>
              <Input
                placeholder="驳回 findings"
                value={rejectFindings}
                onChange={(e) => setRejectFindings(e.target.value)}
                style={{ width: 240 }}
              />
              <Button danger loading={busy} onClick={() => void reviewTask("reject")}>
                驳回返工
              </Button>
            </>
          )}
          {selected.state === "accepted" && (
            <>
              <Button loading={busy} onClick={() => void enqueueMerge()}>
                加入合并队列
              </Button>
              {mergeId && (
                <Button type="primary" loading={busy} onClick={() => void processMerge()}>
                  执行合并
                </Button>
              )}
              <Button loading={busy} onClick={() => void requestMergeApproval()}>
                请求合并审批
              </Button>
            </>
          )}
          {(selected.state === "blocked" ||
            selected.state === "reopened" ||
            selected.state === "failed") && (
            <Button type="primary" loading={busy} onClick={() => void act("重新排队", () => api.requeue(selected.code))}>
              重新排队
            </Button>
          )}
          {!secure ? (
            <Text type="secondary">写操作请使用 https://{window.location.hostname}:9443</Text>
          ) : (
            !token && <Text type="secondary">配置管理员 Token 后可执行写操作</Text>
          )}
        </Space>
      </Card>
    );
  };

  return (
    <Layout className="app-shell">
      <Header className="app-header">
        <div>
          <Title level={3} style={{ color: "#fff", margin: 0 }}>
            MACP Mission Control
          </Title>
          <Text style={{ color: "rgba(255,255,255,0.65)" }}>
            Multi-Agent Coordination Plane · 实时看板与运行控制
          </Text>
        </div>
        <Space size="middle" className="header-actions">
          <Select
            size="small"
            value={project}
            style={{ minWidth: 140 }}
            options={[
              { value: "demo", label: "demo" },
              ...projects
                .filter((p) => p.code !== "demo")
                .map((p) => ({ value: p.code, label: `${p.code} · ${p.name}` })),
            ]}
            onChange={(value) => {
              setProject(value);
              setProjectState(value);
              window.location.reload();
            }}
          />
          <Badge
            status={connected ? "processing" : "error"}
            text={<span style={{ color: "#fff" }}>{connected ? "实时连接" : "连接中断"}</span>}
          />
          <Text style={{ color: "rgba(255,255,255,0.65)" }}>
            最近事件 {fmtTime(lastEventAt ?? overview?.last_event_at)}
          </Text>
          <Button icon={<LoginOutlined />} onClick={() => void loginWithSso()} ghost>
            SSO 登录
          </Button>
          <Button icon={<LockOutlined />} onClick={() => setTokenOpen(true)} ghost>
            {token ? "Token 已配置" : "配置 Token"}
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)} ghost>
            新建任务
          </Button>
          <Button
            icon={<FileTextOutlined />}
            href="/docs"
            target="_blank"
            rel="noreferrer"
            ghost
          >
            API 文档
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} ghost>
            刷新
          </Button>
        </Space>
      </Header>

      <Content className="app-content">
        {error && (
          <Alert
            type="error"
            showIcon
            message="接口请求失败"
            description={error}
            style={{ marginBottom: 16 }}
          />
        )}
        <Alert
          type="info"
          showIcon
          icon={<RobotOutlined />}
          message="所有任务、门禁、Receipt、事件与告警均来自真实 PostgreSQL 与 Git 仓库；写操作使用管理员 Token，Agent 使用按任务签发的作用域 Token。L3 变更合并前必须有人工审批。"
          style={{ marginBottom: 16 }}
        />
        {!secure && (
          <Alert
            type="warning"
            showIcon
            message={`当前是只读 HTTP 入口。写操作请改用 https://${window.location.hostname}:9443（内部证书，首次访问需信任 CA）。`}
            style={{ marginBottom: 16 }}
          />
        )}

        <Row gutter={[16, 16]}>
          {[
            { title: "执行中 WIP", value: overview?.execution_wip ?? 0, icon: <ThunderboltOutlined /> },
            { title: "待验收", value: overview?.review_debt ?? 0, icon: <SafetyCertificateOutlined /> },
            { title: "阻塞", value: overview?.blocked ?? 0, icon: <AlertOutlined /> },
            { title: "已验收", value: overview?.accepted ?? 0, icon: <BranchesOutlined /> },
            { title: "在线 Agent", value: overview?.agents_online ?? 0, icon: <RobotOutlined /> },
            { title: "5 分钟事件", value: overview?.events_5m ?? 0, icon: <ClockCircleOutlined /> },
            { title: "今日模型成本 (CNY)", value: overview?.model_cost_today ?? 0, icon: <ThunderboltOutlined /> },
            { title: "未处理告警", value: overview?.open_alerts ?? 0, icon: <AlertOutlined /> },
          ].map((item) => (
            <Col xs={12} sm={8} lg={3} key={item.title}>
              <Card size="small" className="stat-card">
                <Statistic title={item.title} value={item.value} prefix={item.icon} precision={item.title.includes("成本") ? 4 : undefined} />
              </Card>
            </Col>
          ))}
        </Row>

        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          <Col xs={24} lg={8}>
            <Card size="small" title="任务状态分布">
              <EChart option={stateOptions} />
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" title="事件速率（每分钟）">
              <EChart option={eventRateOption} />
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" title="Workstream WIP">
              <EChart option={workstreamOption} />
            </Card>
          </Col>
        </Row>

        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          <Col xs={24} lg={16}>
            <Card size="small" title="任务" extra={<Text type="secondary">点击任务查看详情与操作</Text>}>
              <Table
                rowKey="code"
                size="small"
                loading={loading}
                columns={columns}
                dataSource={tasks}
                pagination={false}
                scroll={{ x: 760, y: 380 }}
                onRow={(record) => ({ onClick: () => void openTask(record.code) })}
              />
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" title="Agent 名册">
              <List
                size="small"
                dataSource={agents}
                locale={{ emptyText: <Empty description="无 Agent" /> }}
                style={{ maxHeight: 430, overflow: "auto" }}
                renderItem={(agent) => (
                  <List.Item>
                    <Space direction="vertical" size={0} style={{ width: "100%" }}>
                      <Space>
                        <Badge status={AGENT_STATUS[agent.status] ?? "default"} />
                        <Text strong>{agent.code}</Text>
                        <Tag>{agent.role}</Tag>
                      </Space>
                      <Text type="secondary">
                        {agent.status}
                        {agent.current_task ? ` · ${agent.current_task}` : " · 空闲"}
                      </Text>
                    </Space>
                  </List.Item>
                )}
              />
            </Card>
          </Col>
        </Row>

        <Card size="small" style={{ marginTop: 16 }}>
          <Tabs
            items={[
              {
                key: "events",
                label: "实时事件流",
                children: (
                  <List
                    size="small"
                    dataSource={events}
                    locale={{ emptyText: <Empty description="等待事件" /> }}
                    className="event-feed"
                    renderItem={(event) => (
                      <List.Item>
                        <Space direction="vertical" size={0} style={{ width: "100%" }}>
                          <Space wrap>
                            <Tag color="blue">{event.type}</Tag>
                            {event.task && <Text strong>{event.task}</Text>}
                            <Text type="secondary">{fmtTime(event.occurred_at)}</Text>
                            <Text type="secondary">
                              {event.actor_type}:{event.actor_id?.slice(0, 8) ?? "system"}
                            </Text>
                          </Space>
                          <Text type="secondary" ellipsis>
                            {JSON.stringify(event.payload)}
                          </Text>
                        </Space>
                      </List.Item>
                    )}
                  />
                ),
              },
              {
                key: "approvals",
                label: `审批${approvals.length ? ` (${approvals.length})` : ""}`,
                children: (
                  <Table
                    rowKey="id"
                    size="small"
                    dataSource={approvals}
                    columns={approvalColumns}
                    pagination={false}
                    locale={{ emptyText: <Empty description="没有待审批项" /> }}
                  />
                ),
              },
              {
                key: "leases",
                label: `租约${leases.length ? ` (${leases.length})` : ""}`,
                children: (
                  <Table
                    rowKey="id"
                    size="small"
                    dataSource={leases}
                    columns={leaseColumns}
                    pagination={false}
                    locale={{ emptyText: <Empty description="没有活动租约" /> }}
                  />
                ),
              },
              {
                key: "alerts",
                label: `告警${alerts.length ? ` (${alerts.length})` : ""}`,
                children: (
                  <Table
                    rowKey="id"
                    size="small"
                    dataSource={alerts}
                    columns={alertColumns}
                    pagination={false}
                    locale={{ emptyText: <Empty description="没有未处理告警" /> }}
                  />
                ),
              },
              {
                key: "llm",
                label: "LLM 配置",
                children: (
                  <Space direction="vertical" style={{ width: "100%" }}>
                    <Alert
                      type="info"
                      showIcon
                      message="API key 加密存储，保存后不再回显。修改后 worker 在下个任务自动读取，无需改 compose 或重启。当前生效的 worker：worker-deepseek（LLM_CONFIG_FROM_API=1）。"
                    />
                    <Table
                      rowKey="id"
                      size="small"
                      dataSource={llmProviders}
                      columns={llmColumns}
                      pagination={false}
                      locale={{ emptyText: <Empty description="还没有 LLM 配置" /> }}
                    />
                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={() => {
                        setEditingLlm(null);
                        llmForm.resetFields();
                        llmForm.setFieldsValue({
                          agent_code: "worker-deepseek",
                          base_url: "https://api.deepseek.com/v1",
                          model: "deepseek-flash",
                          price_input_per_1m: 2,
                          price_output_per_1m: 8,
                          price_cache_hit_per_1m: 0.04,
                        });
                        setLlmOpen(true);
                      }}
                    >
                      添加 LLM 配置
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </Content>

      <Drawer
        title={selected ? `${selected.code} · ${selected.title}` : "任务详情"}
        width={640}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      >
        {selected && (
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            {actionPanel()}
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="状态">
                <Tag color={STATE_COLORS[selected.state]}>{stateLabel(selected.state)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Workstream">{selected.workstream}</Descriptions.Item>
              <Descriptions.Item label="执行者">{selected.assignee ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="优先级">{selected.priority}</Descriptions.Item>
              <Descriptions.Item label="类型">{selected.kind}</Descriptions.Item>
              <Descriptions.Item label="尝试">{selected.current_attempt}</Descriptions.Item>
              <Descriptions.Item label="更新时间" span={2}>
                {fmtTime(selected.updated_at)}
              </Descriptions.Item>
            </Descriptions>
            {selected.receipt && (
              <Card size="small" title="最新 Receipt">
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="编号">{selected.receipt.code}</Descriptions.Item>
                  <Descriptions.Item label="状态">{selected.receipt.status}</Descriptions.Item>
                  <Descriptions.Item label="风险等级">
                    {String(selected.receipt.body.risk_level ?? "L1")}
                  </Descriptions.Item>
                  <Descriptions.Item label="改动文件">
                    {(selected.receipt.body.changed_paths as string[] | undefined)?.join(", ") ?? "—"}
                  </Descriptions.Item>
                  <Descriptions.Item label="Stale base">
                    {selected.receipt.stale_base ? <Tag color="red">是</Tag> : "否"}
                  </Descriptions.Item>
                </Descriptions>
              </Card>
            )}
            {selected.documents && selected.documents.length > 0 && (
              <Card size="small" title="需求文档">
                <List
                  size="small"
                  dataSource={selected.documents}
                  renderItem={(doc) => (
                    <List.Item
                      actions={[
                        <Button size="small" key="view" onClick={() => void showDocument(doc)}>
                          查看正文
                        </Button>,
                      ]}
                    >
                      <Space>
                        <Tag color="blue">{doc.filename}</Tag>
                        <Text type="secondary">{doc.char_count} 字符</Text>
                        {doc.warnings && doc.warnings.length > 0 && (
                          <Tag color="gold">{doc.warnings.length} 条解析警告</Tag>
                        )}
                      </Space>
                    </List.Item>
                  )}
                />
              </Card>
            )}
            {selected.leases.length > 0 && (
              <Card size="small" title="活动租约">
                <List
                  size="small"
                  dataSource={selected.leases}
                  renderItem={(lease) => (
                    <List.Item>
                      <Space>
                        <Tag>{lease.path_glob}</Tag>
                        <Text type="secondary">到期 {fmtTime(lease.expires_at)}</Text>
                      </Space>
                    </List.Item>
                  )}
                />
              </Card>
            )}
            <div>
              <Title level={5}>最近事件</Title>
              <List
                size="small"
                dataSource={selected.events}
                locale={{ emptyText: <Empty description="无事件" /> }}
                renderItem={(event) => (
                  <List.Item>
                    <Space direction="vertical" size={0}>
                      <Space>
                        <Tag>{event.type}</Tag>
                        <Text type="secondary">{fmtTime(event.occurred_at)}</Text>
                      </Space>
                      <Text type="secondary" ellipsis>
                        {JSON.stringify(event.payload)}
                      </Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Space>
        )}
      </Drawer>

      <Modal
        title="管理员 Token"
        open={tokenOpen}
        onCancel={() => setTokenOpen(false)}
        onOk={async () => {
          const values = await tokenForm.validateFields();
          setToken(values.token.trim());
          setTokenState(values.token.trim());
          setTokenOpen(false);
          message.success("Token 已保存到本地浏览器");
          await refreshAdmin();
        }}
        okText="保存"
      >
        <Alert
          type="warning"
          showIcon
          message="Token 仅保存在本机 localStorage，用于执行写操作。生产环境请通过 TLS 访问并限制终端。"
          style={{ marginBottom: 12 }}
        />
        <Form form={tokenForm} layout="vertical" initialValues={{ token }}>
          <Form.Item name="token" label="Bearer Token" rules={[{ required: true }]}>
            <Input.Password placeholder="MACP write token" />
          </Form.Item>
          <Button
            danger
            onClick={() => {
              setToken("");
              setTokenState("");
              tokenForm.resetFields();
              message.info("已清除本地 Token");
            }}
          >
            清除 Token
          </Button>
        </Form>
      </Modal>

      <Modal
        title="新建任务"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void createTask()}
        confirmLoading={busy}
        okText="创建并生成 Work Order"
        width={560}
      >
        <Form
          form={taskForm}
          layout="vertical"
          initialValues={{
            workstream: "stream-core",
            priority: "P2",
            kind: "slice",
            max_cost: 2,
          }}
        >
          <Form.Item label="需求文档（可选）">
            <Upload.Dragger
              multiple
              showUploadList={false}
              beforeUpload={(file) => {
                void uploadDoc(file);
                return false;
              }}
              disabled={uploading}
            >
              <p className="ant-upload-drag-icon"><UploadOutlined /></p>
              <p>点击或拖入 md / txt / pdf / docx / doc</p>
              <p className="ant-upload-hint">解析后的正文可供 Agent 通过 task_document_read 分段读取；单文件最大 10MB。</p>
            </Upload.Dragger>
            {uploadedDocs.length > 0 && (
              <Space wrap style={{ marginTop: 8 }}>
                {uploadedDocs.map((doc) => (
                  <Tag
                    key={doc.id}
                    color="blue"
                    closable
                    onClose={() =>
                      setUploadedDocs((prev) => prev.filter((item) => item.id !== doc.id))
                    }
                  >
                    {doc.filename} · {doc.char_count} 字
                  </Tag>
                ))}
              </Space>
            )}
          </Form.Item>
          <Form.Item name="code" label="任务编号" rules={[{ required: true, pattern: /^T-\d+$/ }]}>
            <Input placeholder="T-900001" />
          </Form.Item>
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Space style={{ width: "100%" }} align="start">
            <Form.Item name="workstream" label="Workstream" style={{ width: 180 }}>
              <Select
                options={[
                  { value: "stream-core", label: "Core" },
                  { value: "stream-ui", label: "UI" },
                  { value: "stream-infra", label: "Infra" },
                ]}
              />
            </Form.Item>
            <Form.Item name="priority" label="优先级" style={{ width: 110 }}>
              <Select
                options={["P0", "P1", "P2", "P3"].map((v) => ({ value: v, label: v }))}
              />
            </Form.Item>
            <Form.Item name="kind" label="类型" style={{ width: 130 }}>
              <Select
                options={["slice", "fix", "infra", "migration"].map((v) => ({
                  value: v,
                  label: v,
                }))}
              />
            </Form.Item>
            <Form.Item name="max_cost" label="成本上限 (CNY)" style={{ width: 140 }}>
              <Input type="number" min={0} step={0.1} />
            </Form.Item>
          </Space>
          <Text type="secondary">
            Work Order 默认写面为 solution.py / src/** / tests/** / README.md，锚点按当前 main 解析。
          </Text>
        </Form>
      </Modal>

      <Modal
        title={editingLlm ? `编辑 LLM · ${editingLlm.agent_code}` : "添加 LLM 配置"}
        open={llmOpen}
        onCancel={() => {
          setLlmOpen(false);
          setEditingLlm(null);
        }}
        onOk={() => void saveLlm()}
        confirmLoading={busy}
        okText="保存"
        width={600}
      >
        <Alert
          type="warning"
          showIcon
          message="API key 只加密落库，不回显；留空表示保持原 key 不变。base_url 必须是 OpenAI 兼容的 /v1 地址。"
          style={{ marginBottom: 12 }}
        />
        <Form form={llmForm} layout="vertical">
          <Form.Item name="agent_code" label="Agent" rules={[{ required: true }]}>
            <Select
              options={agents
                .filter((a) => a.role === "worker")
                .map((a) => ({ value: a.code, label: `${a.code} · ${a.status}` }))}
            />
          </Form.Item>
          <Form.Item name="name" label="名称">
            <Input placeholder="DeepSeek / OpenAI / vLLM" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          <Form.Item name="model" label="Model" rules={[{ required: true }]}>
            <Input placeholder="deepseek-flash" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key">
            <Input.Password
              placeholder={editingLlm?.api_key_set ? "留空保持原 key" : "sk-..."}
              autoComplete="new-password"
            />
          </Form.Item>
          <Space style={{ width: "100%" }} align="start">
            <Form.Item name="price_input_per_1m" label="输入价/百万" style={{ width: 150 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
            <Form.Item name="price_output_per_1m" label="输出价/百万" style={{ width: 150 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
            <Form.Item name="price_cache_hit_per_1m" label="缓存命中价/百万" style={{ width: 170 }}>
              <Input type="number" step={0.01} />
            </Form.Item>
          </Space>
          <Text type="secondary">
            只有配置了 LLM_CONFIG_FROM_API=1 的 worker 会在下个任务自动使用这里的地址/key。
          </Text>
        </Form>
      </Modal>

      <Modal
        title={`需求文档 · ${docName}`}
        open={docOpen}
        onCancel={() => setDocOpen(false)}
        footer={null}
        width={800}
      >
        <Input.TextArea
          value={docText}
          readOnly
          autoSize={{ minRows: 18, maxRows: 30 }}
          style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}
        />
      </Modal>

    </Layout>
  );
}
