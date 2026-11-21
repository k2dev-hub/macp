import { useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, Drawer, Empty, Form, Input, Modal,
  Row, Select, Space, Steps, Table, Tabs, Tag, Typography, message,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { api, setProject } from "./api";
import type {
  AgentRow, ChainPayload, EvidenceRow, BuildRow, ProjectRow, WorkloadPayload,
  ProvenanceRecord, ProvenanceVerification,
} from "./api";

const { Text, Title } = Typography;

/* ------------------------------------------------------------------ types */

export type UiPageKey = "guide" | "projects" | "artifacts" | "chain" | "agents-admin" | "workload" | "models";

/* ---------------------------------------------------------------- helpers */

const bytes = (value: number | null) => {
  if (value === null || value === undefined) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
};

const when = (iso: string | null) => (iso ? iso.replace("T", " ").slice(0, 19) : "-");

const STATE_COLOR: Record<string, string> = {
  ready: "blue", assigned: "geekblue", in_progress: "processing",
  review: "gold", blocked: "red", merged: "green", done: "green",
  accepted: "green", failed: "red", cancelled: "default", draft: "default",
};

const STATUS_COLOR: Record<string, string> = {
  idle: "blue", working: "processing", waiting_user: "gold",
  blocked: "red", stalled: "orange", offline: "default", dead: "default",
};

function useLoader<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const run = async () => {
    setLoading(true);
    try {
      setData(await load());
      setError("");
    } catch (issue) {
      setError((issue as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void run(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, deps);
  return { data, error, loading, reload: run };
}


/* --------------------------------------------------------------- 0 指南 */

function GuidePage({ onNavigate }: { onNavigate: (page: string) => void }) {
  const projects = useLoader(() => api.uiProjects());
  const providers = useLoader(() => api.llmProviders());
  const tasks = useLoader(() => api.tasks());
  const chain = useLoader(() => api.uiChain());

  const providerCount = providers.data?.length ?? 0;
  const projectCount = projects.data?.length ?? 0;
  const taskCount = tasks.data?.length ?? 0;
  const anchored = chain.data?.totals.anchored ?? 0;

  const items = [
    {
      title: "配置模型供应商",
      done: providerCount > 0,
      detail: "在「设置 · LLM 供应商」填入名称、接口地址、模型名与 Key。一个供应商可以被多个项目复用。",
      action: "去配置", page: "llm",
    },
    {
      title: "创建项目并组建团队",
      done: projectCount > 1,
      detail: "新建项目时选择：每个工作 Agent 用哪家供应商的哪个模型；协调者与观察者是固定角色，不需要模型。",
      action: "去建项目", page: "projects",
    },
    {
      title: "新建任务并派单",
      done: taskCount > 0,
      detail: "任务声明可改动的路径、必须通过的门禁；派单后 Agent 认领，在隔离环境里执行。",
      action: "去派单", page: "tasks",
    },
    {
      title: "查看审计与上链",
      done: anchored > 0,
      detail: "每次构建生成溯源记录，指纹钉在 Hyperledger Fabric 上；「审计」里可看事件流、账本与一致性。",
      action: "看审计", page: "audit",
    },
  ];

  return (
    <div className="page">
      <Title level={4}>使用指引</Title>
      <Text type="secondary">按顺序走一遍，四个步骤都打勾，系统就跑通了。</Text>
      <Card style={{ marginTop: 12 }}>
        {items.map((item, index) => (
          <div key={item.title} style={{
            display: "flex", gap: 16, padding: "14px 0",
            borderBottom: index === items.length - 1 ? "none" : "1px solid #f0f2f5",
          }}>
            <div style={{ paddingTop: 2 }}>
              {item.done ? <Tag color="green">已完成</Tag> : <Tag>待开始</Tag>}
            </div>
            <div style={{ flex: 1 }}>
              <Text strong>{index + 1}. {item.title}</Text>
              <div><Text type="secondary" style={{ fontSize: 12 }}>{item.detail}</Text></div>
            </div>
            <Button size="small" onClick={() => onNavigate(item.page)}>{item.action}</Button>
          </div>
        ))}
      </Card>
      <Card title="两条常用路径" style={{ marginTop: 16 }}>
        <Space direction="vertical" size={6}>
          <Text>· 想快速看效果：直接「新建任务」，用默认团队跑一单，然后到「审计 · 事件流」看过程。</Text>
          <Text>· 想接入自己的模型：先配供应商，再新建项目并在向导里为每个 Agent 选模型。</Text>
        </Space>
      </Card>
    </div>
  );
}

/* --------------------------------------------------------------- 1 项目 */

function ProjectsPage({ onNavigate }: { onNavigate: (page: string) => void }) {
  const { data, error, loading, reload } = useLoader(() => api.uiProjects());
  const providers = useLoader(() => api.llmProviders());
  const [wizard, setWizard] = useState(false);
  const [step, setStep] = useState(0);
  const [editing, setEditing] = useState<ProjectRow | null>(null);
  const [form] = Form.useForm();
  const [team, setTeam] = useState<Array<{ code: string; provider_id?: string; model?: string }>>([
    { code: "worker-1" },
    { code: "worker-2" },
    { code: "worker-3" },
  ]);

  const providerOptions = useMemo(() => {
    const seen = new Set<string>();
    const list: Array<{ value: string; label: string }> = [];
    for (const provider of providers.data ?? []) {
      const key = `${provider.name}|${provider.model}|${provider.base_url}`;
      if (seen.has(key)) continue;
      seen.add(key);
      list.push({ value: provider.id, label: `${provider.name} · ${provider.model}` });
    }
    return list;
  }, [providers.data]);

  const openWizard = () => {
    form.resetFields();
    setStep(0);
    setTeam([{ code: "worker-1" }, { code: "worker-2" }, { code: "worker-3" }]);
    setWizard(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    const payload = team
      .filter((member) => member.provider_id)
      .map((member) => ({ code: member.code, provider_id: member.provider_id as string, model: member.model }));
    await api.createProject({ code: values.code, name: values.name, team: payload.length ? payload : undefined });
    message.success("项目已创建，团队已按所选模型组建");
    setWizard(false);
    window.dispatchEvent(new Event("macp:projects-changed"));
    await reload();
  };

  const columns: ColumnsType<ProjectRow> = [
    { title: "编码", dataIndex: "code", width: 130,
      render: (code: string, row) => (
        <Space>
          <Text strong>{code}</Text>
          {row.current ? <Tag color="green">当前</Tag> : null}
        </Space>
      ) },
    { title: "名称", dataIndex: "name" },
    { title: "任务", dataIndex: "tasks", width: 80, align: "right" },
    { title: "Agent", dataIndex: "agents", width: 80, align: "right" },
    { title: "创建时间", dataIndex: "created_at", width: 180, render: when },
    { title: "操作", width: 200, render: (_, row) => (
      <Space>
        <Button size="small" onClick={() => { setEditing(row); form.setFieldsValue({ name: row.name }); }}>改名</Button>
        <Button size="small" disabled={row.current}
          onClick={() => { setProject(row.code); window.location.reload(); }}>切换</Button>
      </Space>
    ) },
  ];

  return (
    <div className="page">
      <Title level={4}>项目管理</Title>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} /> : null}
      <Card extra={<Space><Button icon={<ReloadOutlined />} onClick={reload}>刷新</Button>
        <Button type="primary" onClick={openWizard}>新建项目</Button></Space>}>
        <Table rowKey="code" size="small" loading={loading} columns={columns} dataSource={data ?? []} pagination={false} />
      </Card>

      <Modal title="新建项目" width={640} open={wizard} onCancel={() => setWizard(false)}
        onOk={() => (step < 2 ? setStep(step + 1) : submit())}
        maskClosable={false}
        okText={step < 2 ? "下一步" : "创建"}
        cancelText={step === 0 ? "取消" : "上一步"}
        footer={[
          <Button key="back" onClick={() => (step === 0 ? setWizard(false) : setStep(step - 1))}>
            {step === 0 ? "取消" : "上一步"}
          </Button>,
          <Button key="next" type="primary" onClick={async () => {
            if (step === 0) {
              // 基本信息填完才允许进下一步，避免一路点到确认页才发现没填
              try {
                await form.validateFields(["code", "name"]);
              } catch {
                return;
              }
            }
            if (step < 2) setStep(step + 1);
            else await submit();
          }}>
            {step < 2 ? "下一步" : "创建项目"}
          </Button>,
        ]}>
        <Steps size="small" current={step} style={{ marginBottom: 20 }}
          items={[{ title: "基本信息" }, { title: "组建团队" }, { title: "确认" }]} />

        <Form form={form} layout="vertical" style={{ display: step === 0 ? "block" : "none" }}>
          <Form.Item name="code" label="项目编码" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9-]{1,31}$/, message: "小写字母开头，可含数字与连字符" }]}>
            <Input placeholder="例如 demo-2" />
          </Form.Item>
          <Form.Item name="name" label="项目名称" rules={[{ required: true }]}>
            <Input placeholder="例如 演示项目" />
          </Form.Item>
        </Form>

        {step === 1 ? (
          <div>
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message="协调者与观察者是固定角色，不需要模型；下面为每个工作 Agent 选择模型来源。" />
            {providerOptions.length === 0 ? (
              <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                message="还没有配置 LLM 供应商"
                action={<Button size="small" onClick={() => { setWizard(false); onNavigate("llm"); }}>去配置</Button>} />
            ) : null}
            {team.map((member, index) => (
              <Row gutter={8} key={index} style={{ marginBottom: 10 }}>
                <Col span={6}>
                  <Input value={member.code} onChange={(event) => {
                    const next = [...team];
                    next[index] = { ...next[index], code: event.target.value };
                    setTeam(next);
                  }} placeholder="worker-1" />
                </Col>
                <Col span={10}>
                  <Select style={{ width: "100%" }} placeholder="选择模型来源" allowClear
                    value={member.provider_id}
                    options={providerOptions}
                    onChange={(value) => {
                      const next = [...team];
                      next[index] = { ...next[index], provider_id: value };
                      setTeam(next);
                    }} />
                </Col>
                <Col span={6}>
                  <Input placeholder="模型名（可选）" value={member.model}
                    onChange={(event) => {
                      const next = [...team];
                      next[index] = { ...next[index], model: event.target.value };
                      setTeam(next);
                    }} />
                </Col>
                <Col span={2}>
                  <Button danger onClick={() => setTeam(team.filter((_, i) => i !== index))}>删</Button>
                </Col>
              </Row>
            ))}
            <Button onClick={() => setTeam([...team, { code: `worker-${team.length + 1}` }])}>+ 再加一个 Agent</Button>
          </div>
        ) : null}

        {step === 2 ? (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="编码">{form.getFieldValue("code")}</Descriptions.Item>
            <Descriptions.Item label="名称">{form.getFieldValue("name")}</Descriptions.Item>
            <Descriptions.Item label="团队">
              <Space direction="vertical" size={2}>
                <Text>coordinator-1 · 协调者（无模型）</Text>
                <Text>observer-1 · 观察者（无模型）</Text>
                {team.map((member) => {
                  const provider = (providers.data ?? []).find((item) => item.id === member.provider_id);
                  return (
                    <Text key={member.code}>
                      {member.code} · {provider ? `${provider.name} / ${member.model || provider.model}` : "未选模型"}
                    </Text>
                  );
                })}
              </Space>
            </Descriptions.Item>
          </Descriptions>
        ) : null}
      </Modal>

      <Modal title={`重命名 ${editing?.code ?? ""}`} open={!!editing} onCancel={() => setEditing(null)}
        onOk={async () => {
          const values = await form.validateFields();
          if (!editing) return;
          await api.renameProject(editing.code, values.name);
          message.success("已保存");
          setEditing(null);
          window.dispatchEvent(new Event("macp:projects-changed"));
          await reload();
        }} okText="保存">
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

/* --------------------------------------------------------------- 2 产物 */

function ArtifactsPage() {
  const { data, error, loading, reload } = useLoader(() => api.uiArtifacts(60));

  const evidenceColumns: ColumnsType<EvidenceRow> = [
    { title: "类型", dataIndex: "kind", width: 90, render: (kind: string) => <Tag>{kind}</Tag> },
    { title: "任务", dataIndex: "task", width: 130, render: (v: string | null) => v ?? "-" },
    { title: "门禁", dataIndex: "gate", width: 110, render: (v: string | null) => v ?? "-" },
    { title: "退出码", dataIndex: "exit_code", width: 80, align: "right",
      render: (v: number | null) => (v === null ? "-" : <Text type={v === 0 ? "success" : "danger"}>{v}</Text>) },
    { title: "大小", dataIndex: "size_bytes", width: 90, align: "right", render: bytes },
    { title: "摘要", dataIndex: "sha256", width: 150, render: (v: string) => <Text code>{v.slice(0, 12)}</Text> },
    { title: "时间", dataIndex: "created_at", width: 170, render: when },
    { title: "下载", width: 90, render: (_, row) => (
      <Button size="small" disabled={!row.downloadable}
        onClick={() => { void api.downloadEvidence(row.id).catch((e) => message.error(String(e))); }}>
        {row.downloadable ? "下载" : "已过期"}
      </Button>
    ) },
  ];

  const buildColumns: ColumnsType<BuildRow> = [
    { title: "构建编号", dataIndex: "build_id", width: 190, render: (v: string) => <Text code>{v}</Text> },
    { title: "任务", dataIndex: "task", width: 130 },
    { title: "产物", dataIndex: "artifact_name", width: 140, render: (v: string | null) => v ?? "-" },
    { title: "大小", dataIndex: "artifact_size", width: 90, align: "right", render: bytes },
    { title: "上链", dataIndex: "fabric_status", width: 100,
      render: (v: string | null) => (v === "anchored" ? <Tag color="purple">已上链</Tag>
        : v === "failed" ? <Tag color="red">失败</Tag> : <Tag>未上链</Tag>) },
    { title: "验证 / 标签包", width: 210, render: (_, row) => (
      <Space>
        <Button size="small" onClick={() => window.open(`/verify/${row.build_id}`, "_blank")}>验证页</Button>
        <Button size="small"
          onClick={() => { void api.downloadStamp(row.build_id).catch((e) => message.error(String(e))); }}>标签包</Button>
      </Space>
    ) },
  ];

  return (
    <div className="page">
      <Title level={4}>产物与证据</Title>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} /> : null}
      <Card extra={<Button icon={<ReloadOutlined />} onClick={reload}>刷新</Button>}>
        <Tabs
          items={[
            {
              key: "builds", label: `构建溯源 ${data?.builds.length ?? 0}`,
              children: <Table rowKey="build_id" size="small" loading={loading} dataSource={data?.builds ?? []}
                columns={buildColumns} pagination={false} />,
            },
            {
              key: "evidence", label: `证据文件 ${data?.evidence.length ?? 0}`,
              children: <Table rowKey="id" size="small" loading={loading} dataSource={data?.evidence ?? []}
                columns={evidenceColumns} pagination={false} />,
            },
          ]}
        />
      </Card>
    </div>
  );
}

/* --------------------------------------------------------------- 3 上链 */

function ChainPage() {
  const { data, error, loading, reload } = useLoader(() => api.uiChain());
  const [detailFor, setDetailFor] = useState<string | null>(null);
  const [detail, setDetail] = useState<{
    record: ProvenanceRecord;
    verification: ProvenanceVerification;
    anchor: { found: boolean; record?: Record<string, unknown>; fingerprint_matches?: boolean };
  } | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);

  const openDetail = async (buildId: string) => {
    setDetailFor(buildId);
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    try {
      const [main, anchor] = await Promise.all([
        api.provenance(buildId),
        api.chainAnchor(buildId).catch(() => ({ found: false })),
      ]);
      setDetail({ record: main.record, verification: main.verification, anchor });
    } catch (issue) {
      setDetailError((issue as Error).message);
    } finally {
      setDetailLoading(false);
    }
  };

  const columns: ColumnsType<ChainPayload["builds"][number]> = [
    { title: "构建编号", dataIndex: "build_id", width: 190, render: (v: string) => <Text code>{v}</Text> },
    { title: "任务", dataIndex: "task", width: 120 },
    { title: "状态", dataIndex: "status", width: 100,
      render: (v: string | null) => (v === "anchored" ? <Tag color="purple">已上链</Tag>
        : v === "failed" ? <Tag color="red">失败</Tag> : <Tag>未上链</Tag>) },
    { title: "交易", dataIndex: "tx_id", width: 160, render: (v: string | null) => (v ? <Text code>{v.slice(0, 12)}</Text> : "-") },
    { title: "区块", dataIndex: "block", width: 80, align: "right", render: (v: number | null) => v ?? "-" },
    { title: "上链时间", dataIndex: "anchored_at", width: 170, render: when },
    {
      title: "详情",
      width: 90,
      render: (_, row) => (
        <Button size="small" onClick={() => void openDetail(row.build_id)}>查看</Button>
      ),
    },
  ];

  const fabric = data?.fabric;
  return (
    <div className="page">
      <Title level={4}>上链（Hyperledger Fabric）</Title>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} /> : null}
      {fabric && !fabric.enabled ? (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="Fabric 未启用" description="config/macp.toml 的 [fabric] enabled 为 false，或桥接不可达。" />
      ) : null}
      {fabric?.error ? <Alert type="error" showIcon style={{ marginBottom: 12 }} message={fabric.error} /> : null}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title="本机构建记录" extra={<Button size="small" onClick={reload}>刷新</Button>}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="构建记录">{data?.totals.builds ?? 0}</Descriptions.Item>
              <Descriptions.Item label="已上链">{data?.totals.anchored ?? 0}</Descriptions.Item>
              <Descriptions.Item label="上链失败">{data?.totals.failed ?? 0}</Descriptions.Item>
              <Descriptions.Item label="未上链">{data?.totals.unanchored ?? 0}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="Hyperledger Fabric" extra={<Space>
            <Button size="small" onClick={() => window.open("/chain", "_blank")}>账本页</Button>
            <Button size="small" onClick={() => window.open("/audit", "_blank")}>审计看板</Button>
          </Space>}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="通道">{fabric?.channel ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="链码">{fabric?.chaincode ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="区块高度">{fabric?.block_height ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="链上锚定">{fabric?.total ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="最近锚定" span={2}>{when(fabric?.last_anchored ?? null)}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Card title="最近构建" style={{ marginBottom: 16 }} extra={<Text type="secondary">点「查看」看完整记录与链上读回</Text>}>
        <Table rowKey="build_id" size="small" loading={loading}
          dataSource={data?.builds ?? []} columns={columns} pagination={false}
          onRow={(row) => ({ onClick: () => void openDetail(row.build_id), style: { cursor: "pointer" } })} />
      </Card>

      <Drawer
        width={720}
        open={detailFor !== null}
        onClose={() => setDetailFor(null)}
        title={`构建详情 · ${detailFor ?? ""}`}
        extra={<Space>
          <Button size="small" onClick={() => detailFor && window.open(`/verify/${detailFor}`, "_blank")}>验证页</Button>
          <Button size="small" onClick={() => detailFor && void api.downloadStamp(detailFor).catch((e) => message.error(String(e)))}>标签包</Button>
        </Space>}
      >
        {detailLoading ? <Empty description="读取中…" /> : null}
        {detailError ? <Alert type="error" showIcon message={detailError} style={{ marginBottom: 12 }} /> : null}
        {detail ? (
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Card size="small" title="记录">
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="构建编号">{detail.record.build_id}</Descriptions.Item>
                <Descriptions.Item label="任务">{detail.record.task_code} · 第 {detail.record.attempt} 次尝试</Descriptions.Item>
                <Descriptions.Item label="门禁 / 画像">
                  {detail.record.gate} · {detail.record.profile ?? "-"} · {detail.record.platform ?? "-"}
                </Descriptions.Item>
                <Descriptions.Item label="产物">
                  {detail.record.artifact_name ?? "-"}（{detail.record.artifact_size ?? "-"} 字节）
                  <div><Text code copyable>{detail.record.artifact_sha256 ?? "-"}</Text></div>
                </Descriptions.Item>
                <Descriptions.Item label="源码">{detail.record.source_commit ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="构建命令">
                  <Text code>{(detail.record.build_command ?? []).join(" ") || "-"}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="执行环境">
                  {String((detail.record.build_env ?? {}).runner_os ?? "-")} · {(detail.record.build_env ?? {}).vm ? String((detail.record.build_env ?? {}).vm) : "-"}
                </Descriptions.Item>
                <Descriptions.Item label="构建人">{detail.record.builder ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="构建时间">{when(detail.record.created_at)}</Descriptions.Item>
                <Descriptions.Item label="记录指纹">
                  <Text code copyable>{detail.record.fingerprint}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="记录签名">
                  <Text code>{(detail.record.signature ?? "-").slice(0, 48)}…</Text>
                </Descriptions.Item>
              </Descriptions>
            </Card>

            <Card size="small" title="三项校验">
              {(detail.verification.checks ?? []).map((item) => (
                <div key={item.check} style={{ marginBottom: 6 }}>
                  <Tag color={item.ok ? "green" : "red"}>{item.ok ? "通过" : "失败"}</Tag>
                  <Text strong>{item.check}</Text>
                  <Text type="secondary" style={{ marginLeft: 8 }}>{item.detail ?? ""}</Text>
                </div>
              ))}
              <div style={{ marginTop: 8 }}>
                <Tag color={detail.verification.verified ? "green" : "red"}>
                  {detail.verification.verified ? "记录完整" : "记录异常"}
                </Tag>
                <Text type="secondary">重算指纹 {detail.verification.recomputed_fingerprint?.slice(0, 16) ?? "-"}…</Text>
              </div>
            </Card>

            <Card size="small" title="链上读回（Hyperledger Fabric）">
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="状态">
                  {detail.record.fabric_status === "anchored"
                    ? <Tag color="purple">已上链</Tag>
                    : <Tag color="red">未上链</Tag>}
                  {detail.anchor.fingerprint_matches === true
                    ? <Tag color="green" style={{ marginLeft: 8 }}>链上指纹一致</Tag>
                    : detail.anchor.found
                      ? <Tag color="red" style={{ marginLeft: 8 }}>链上指纹不一致</Tag>
                      : <Tag style={{ marginLeft: 8 }}>链上查不到</Tag>}
                </Descriptions.Item>
                <Descriptions.Item label="交易号">
                  <Text code copyable>{detail.record.fabric_tx_id ?? "-"}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="区块高度">{detail.record.fabric_block ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="上链时间">{when(detail.record.fabric_anchored)}</Descriptions.Item>
                <Descriptions.Item label="链上指纹">
                  <Text code>{String((detail.anchor.record ?? {}).fingerprint ?? "-")}</Text>
                </Descriptions.Item>
              </Descriptions>
            </Card>
          </Space>
        ) : null}
      </Drawer>

      <Card title="审计锚点（每日链头）">
        <Table rowKey="day" size="small" loading={loading} pagination={false}
          dataSource={data?.audit_anchors ?? []}
          columns={[
            { title: "日期", dataIndex: "day", width: 130 },
            { title: "覆盖到序号", dataIndex: "seq", width: 140, align: "right" },
            { title: "链头哈希", dataIndex: "hash", render: (v: string) => <Text code>{v.slice(0, 24)}…</Text> },
          ]} />
      </Card>
    </div>
  );
}

/* --------------------------------------------------------------- 4 Agents */

function AgentsPage() {
  const { data, error, loading, reload } = useLoader(() => api.uiAgents());
  const [editing, setEditing] = useState<AgentRow | null>(null);
  const [onlyOnline, setOnlyOnline] = useState(false);
  const [form] = Form.useForm();
  const providers = data?.providers ?? [];
  const rows = useMemo(() => {
    const list = [...(data?.agents ?? [])];
    list.sort((a, b) => Number(Boolean(b.online)) - Number(Boolean(a.online)));
    return onlyOnline ? list.filter((row) => row.online) : list;
  }, [data, onlyOnline]);

  const open = (row: AgentRow) => {
    setEditing(row);
    form.setFieldsValue({
      status: row.status,
      capabilities: row.capabilities,
      allowed_paths: row.allowed_paths,
      provider: providers.find((p) => p.agent_code === row.code)?.id,
      model: String(row.model_policy?.model ?? ""),
    });
  };

  const submit = async () => {
    const values = await form.validateFields();
    if (!editing) return;
    const chosen = providers.find((p) => p.id === values.provider);
    await api.updateAgent(editing.code, {
      status: values.status,
      capabilities: values.capabilities ?? [],
      allowed_paths: values.allowed_paths ?? [],
      model_policy: {
        ...(editing.model_policy ?? {}),
        provider: chosen?.name ?? null,
        base_url: chosen?.base_url ?? null,
        model: values.model || chosen?.model || null,
      },
    });
    message.success("Agent 已更新");
    setEditing(null);
    await reload();
  };

  const columns: ColumnsType<AgentRow> = [
    { title: "Agent", dataIndex: "code", width: 150,
      render: (v: string) => <Text strong>{v}</Text> },
    { title: "在跑", dataIndex: "online", width: 90,
      render: (online: boolean, row) => (online
        ? <Tag color="green">在线</Tag>
        : <Tag>{row.heartbeat_age_seconds == null ? "未接入" : "离线"}</Tag>) },
    { title: "角色", dataIndex: "role", width: 100, render: (v: string) => <Tag>{v}</Tag> },
    { title: "状态", dataIndex: "status", width: 110,
      render: (v: string) => <Tag color={STATUS_COLOR[v] ?? "default"}>{v}</Tag> },
    { title: "当前任务", dataIndex: "current_task", width: 130, render: (v: string | null) => v ?? "-" },
    { title: "模型来源", width: 210,
      render: (_, row) => (row.provider
        ? <Space direction="vertical" size={0}>
            <Text>{row.provider}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>{row.provider_model}</Text>
          </Space>
        : <Text type="secondary">{String(row.model_policy?.model ?? "未配置")}</Text>) },
    { title: "能力", dataIndex: "capabilities",
      render: (list: string[]) => (list.length ? list.map((c) => <Tag key={c}>{c}</Tag>) : <Text type="secondary">-</Text>) },
    { title: "操作", width: 90, render: (_, row) => <Button size="small" onClick={() => open(row)}>配置</Button> },
  ];

  return (
    <div className="page">
      <Title level={4}>Agent 管理</Title>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} /> : null}
      <Card extra={<Space>
        <Button size="small" onClick={() => setOnlyOnline((value) => !value)}>
          {onlyOnline ? "显示全部" : "只看在跑"}
        </Button>
        <Button icon={<ReloadOutlined />} onClick={reload}>刷新</Button>
      </Space>}>
        <Table rowKey="code" size="small" loading={loading} columns={columns}
          dataSource={rows} pagination={false} />
      </Card>

      <Drawer title={`配置 ${editing?.code ?? ""}`} width={430} open={!!editing} onClose={() => setEditing(null)}
        extra={<Button type="primary" onClick={submit}>保存</Button>}>
        <Form form={form} layout="vertical">
          <Form.Item name="status" label="状态">
            <Select options={["idle", "working", "waiting_user", "blocked", "stalled", "offline"].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item name="provider" label="模型来源（LLM 供应商）"
            extra={providers.length ? "" : "还没有供应商，先到「LLM 配置」添加"}>
            <Select allowClear placeholder="选择已配置的供应商"
              options={providers.map((p) => ({ value: p.id, label: `${p.name} · ${p.model}` }))} />
          </Form.Item>
          <Form.Item name="model" label="模型名（留空则用供应商默认）">
            <Input placeholder="例如 deepseek-chat" />
          </Form.Item>
          <Form.Item name="capabilities" label="能力标签">
            <Select mode="tags" placeholder="例如 shell、python、frontend" />
          </Form.Item>
          <Form.Item name="allowed_paths" label="允许改动的路径">
            <Select mode="tags" placeholder="例如 src/**、tests/**" />
          </Form.Item>
        </Form>
      </Drawer>
    </div>
  );
}

/* --------------------------------------------------------------- 5 分工 */

function WorkloadPage() {
  const { data, error, loading, reload } = useLoader(() => api.uiWorkload());

  const stateColumns: ColumnsType<{ state: string; count: number }> = [
    { title: "状态", dataIndex: "state", render: (v: string) => <Tag color={STATE_COLOR[v] ?? "default"}>{v}</Tag> },
    { title: "数量", dataIndex: "count", align: "right" },
  ];

  const taskColumns: ColumnsType<WorkloadPayload["in_flight"][number]> = [
    { title: "任务", dataIndex: "code", width: 130 },
    { title: "标题", dataIndex: "title", ellipsis: true },
    { title: "状态", dataIndex: "state", width: 110,
      render: (v: string) => <Tag color={STATE_COLOR[v] ?? "default"}>{v}</Tag> },
    { title: "负责 Agent", dataIndex: "agent", width: 140, render: (v: string | null) => v ?? <Text type="secondary">未派单</Text> },
    { title: "更新时间", dataIndex: "updated_at", width: 170, render: when },
  ];

  return (
    <div className="page">
      <Title level={4}>分工与在办</Title>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} /> : null}
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={15}>
          <Card title="每个 Agent 在做什么" loading={loading}
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={reload}>刷新</Button>}>
            <Row gutter={[12, 12]}>
              {(data?.agents ?? []).map((agent) => (
                <Col key={agent.code} xs={24} md={12}>
                  <Card size="small" style={{ borderColor: "#eaecf0" }}>
                    <Space direction="vertical" size={4} style={{ width: "100%" }}>
                      <Space>
                        <Text strong>{agent.code}</Text>
                        <Tag color={agent.online ? "green" : undefined}>{agent.online ? "在线" : "离线"}</Tag>
                        <Tag color={STATUS_COLOR[agent.status] ?? "default"}>{agent.status}</Tag>
                        <Tag>{agent.role}</Tag>
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        在办任务：{agent.current_task ?? "无"}
                        {agent.current_state ? ` · ${agent.current_state}` : ""}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        活跃分配 {agent.active_assignments} · 24h 分配 {agent.assignments_24h} · 24h 阻塞 {agent.blocked_24h}
                      </Text>
                      <div>
                        {agent.capabilities.length
                          ? agent.capabilities.map((c) => <Tag key={c} color="blue">{c}</Tag>)
                          : <Text type="secondary" style={{ fontSize: 12 }}>未声明能力</Text>}
                      </div>
                    </Space>
                  </Card>
                </Col>
              ))}
              {!loading && !(data?.agents ?? []).length ? <Col span={24}><Empty /></Col> : null}
            </Row>
          </Card>
        </Col>
        <Col xs={24} xl={9}>
          <Card title="任务状态分布" loading={loading} style={{ marginBottom: 16 }}>
            <Table rowKey="state" size="small" pagination={false}
              dataSource={data?.by_state ?? []} columns={stateColumns} />
          </Card>
          <Card title="在办任务" loading={loading}>
            <Table rowKey="code" size="small" pagination={false}
              dataSource={data?.in_flight ?? []} columns={taskColumns} />
          </Card>
        </Col>
      </Row>
    </div>
  );
}

/* --------------------------------------------------------------- 6 模型 */

function ModelsPage() {
  const { data, error, loading, reload } = useLoader(() => api.uiAgents());
  const agents = useMemo(() => data?.agents ?? [], [data]);
  const providers = data?.providers ?? [];
  const [form] = Form.useForm();
  const [editing, setEditing] = useState<AgentRow | null>(null);

  const columns: ColumnsType<AgentRow> = [
    { title: "Agent", dataIndex: "code", width: 150 },
    { title: "角色", dataIndex: "role", width: 100, render: (v: string) => <Tag>{v}</Tag> },
    { title: "来源", dataIndex: "provider", width: 180,
      render: (v: string | null) => (v ? <Text>{v}</Text> : <Text type="secondary">默认</Text>) },
    { title: "模型", width: 180,
      render: (_, row) => <Text code>{row.provider_model ?? String(row.model_policy?.model ?? "-")}</Text> },
    { title: "策略", dataIndex: "model_policy",
      render: (policy: Record<string, unknown>) => {
        const shown = Object.entries(policy ?? {}).filter(([k]) => k !== "provider" && k !== "base_url");
        return shown.length ? shown.map(([k, v]) => <Tag key={k}>{`${k}=${String(v)}`}</Tag>) : <Text type="secondary">-</Text>;
      } },
    { title: "操作", width: 90, render: (_, row) => (
      <Button size="small" onClick={() => {
        setEditing(row);
        form.setFieldsValue({
          provider: providers.find((p) => p.agent_code === row.code)?.id,
          model: String(row.model_policy?.model ?? ""),
          temperature: String(row.model_policy?.temperature ?? ""),
        });
      }}>指派</Button>
    ) },
  ];

  const submit = async () => {
    const values = await form.validateFields();
    if (!editing) return;
    const chosen = providers.find((p) => p.id === values.provider);
    await api.updateAgent(editing.code, {
      model_policy: {
        provider: chosen?.name ?? null,
        base_url: chosen?.base_url ?? null,
        model: values.model || chosen?.model || null,
        ...(values.temperature ? { temperature: Number(values.temperature) } : {}),
      },
    });
    message.success("已指派");
    setEditing(null);
    await reload();
  };

  return (
    <div className="page">
      <Title level={4}>模型指派</Title>
      <Text type="secondary">为 Agent 指定模型来源；供应商在「设置 · LLM 供应商」维护。</Text>
      {error ? <Alert type="error" message={error} showIcon style={{ marginBottom: 12, marginTop: 12 }} /> : null}
      <Card style={{ marginTop: 12 }} extra={<Button icon={<ReloadOutlined />} onClick={reload}>刷新</Button>}>
        <Table rowKey="code" size="small" loading={loading} columns={columns} dataSource={agents} pagination={false} />
      </Card>

      <Modal title={`指派模型 · ${editing?.code ?? ""}`} open={!!editing} onCancel={() => setEditing(null)} onOk={submit} okText="保存">
        <Form form={form} layout="vertical">
          <Form.Item name="provider" label="模型来源">
            <Select allowClear placeholder="选择供应商"
              options={providers.map((p) => ({ value: p.id, label: `${p.name} · ${p.model}` }))} />
          </Form.Item>
          <Form.Item name="model" label="模型名">
            <Input placeholder="留空则用供应商默认" />
          </Form.Item>
          <Form.Item name="temperature" label="temperature（可选）">
            <Input placeholder="例如 0.2" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

/* -------------------------------------------------------------- dispatch */

export function AdminPage({ page, onNavigate }: { page: UiPageKey; onNavigate?: (page: string) => void }) {
  if (page === "guide") return <GuidePage onNavigate={onNavigate ?? (() => undefined)} />;
  if (page === "projects") return <ProjectsPage onNavigate={onNavigate ?? (() => undefined)} />;
  if (page === "artifacts") return <ArtifactsPage />;
  if (page === "chain") return <ChainPage />;
  if (page === "agents-admin") return <AgentsPage />;
  if (page === "workload") return <WorkloadPage />;
  return <ModelsPage />;
}

export const adminPageKeys: UiPageKey[] = ["guide", "projects", "artifacts", "chain", "agents-admin", "workload", "models"];

