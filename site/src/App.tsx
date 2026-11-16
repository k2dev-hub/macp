import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Row,
  Space,
  Steps,
  Table,
  Tag,
  Timeline,
  Typography,
} from "antd";
import {
  AuditOutlined,
  BranchesOutlined,
  BuildOutlined,
  CloudServerOutlined,
  DeploymentUnitOutlined,
  FileProtectOutlined,
  GithubOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

const { Title, Paragraph, Text } = Typography;

const REPO = "https://github.com/k2dev-hub/macp";

const pillars = [
  {
    icon: <BranchesOutlined />,
    title: "任务图是真相源",
    body: "工作项声明写面、验收门禁和执行画像；代码仓只是产物，不是事实本身。",
  },
  {
    icon: <SafetyCertificateOutlined />,
    title: "门禁绑定执行环境",
    body: "在哪种环境里构建，就在哪种环境里复验。Linux、Android、Windows 各有自己的门禁。",
  },
  {
    icon: <FileProtectOutlined />,
    title: "交付可验证",
    body: "候选 ref → 门禁结果 → Ed25519 签名回执 → 合并。任何人拿公钥都能独立复算。",
  },
  {
    icon: <AuditOutlined />,
    title: "历史可追溯",
    body: "追加式事件链 + 每日锚点 + 异地镜像，改一条历史就对不上哈希。",
  },
];

const profiles = [
  {
    key: "sandbox",
    platform: <Tag color="default">sandbox</Tag>,
    carrier: "bwrap 沙箱",
    use: "快速试跑、解释型脚本",
  },
  {
    key: "linux",
    platform: <Tag color="blue">linux</Tag>,
    carrier: "一次性 Debian VM",
    use: "make / cargo / 真实 ELF 产物",
  },
  {
    key: "android",
    platform: <Tag color="green">android</Tag>,
    carrier: "一次性 Android VM",
    use: "gradle assembleDebug → 签名 APK",
  },
  {
    key: "windows",
    platform: <Tag color="geekblue">windows</Tag>,
    carrier: "Windows LTSC 构建机",
    use: "cmd / MSBuild → PE 产物",
  },
];

export default function App() {
  return (
    <>
      <header className="site-header">
        <div className="wrap">
          <a className="brand" href="#top" aria-label="MACP 首页">
            <span className="brand-mark">M</span>
            MACP
          </a>
          <nav className="nav" aria-label="主导航">
            <a href="#why">它解决什么</a>
            <a href="#flow">交付路径</a>
            <a href="#profiles">执行画像</a>
            <a href="#provenance">构建溯源</a>
            <a href="#start">快速开始</a>
          </nav>
          <Button type="primary" href={REPO} icon={<GithubOutlined />}>
            GitHub
          </Button>
        </div>
      </header>

      <main id="top">
        <section className="hero">
          <div className="wrap">
            <Title level={1} style={{ marginTop: 0 }}>
              多 Agent 写代码，
              <br />
              也要能被审计
            </Title>
            <Paragraph className="lead">
              MACP 是一条可审计的交付流水线：任务图是唯一真相源，门禁在真实环境里跑构建，
              每次交付带签名回执，每条事件串进哈希链，产物指纹还能锚定到联盟链。
            </Paragraph>
            <Paragraph className="sub">
              MIT 开源 · Python + TypeScript · 一条命令跑起来 · 不需要任何模型 API key 也能看到完整链路
            </Paragraph>
            <div className="hero-actions">
              <Button type="primary" size="large" href="#start">
                五分钟跑起来
              </Button>
              <Button size="large" href={`${REPO}#readme`} icon={<GithubOutlined />}>
                看源码
              </Button>
              <Text type="secondary">v0.3 · 12 项发布检查全绿</Text>
            </div>
          </div>
        </section>

        <section id="why" className="alt">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>它解决什么</Title>
              <Paragraph>
                多 Agent 系统最容易失控的地方不是模型能力，而是「谁在什么时候、基于什么证据、改动了什么」。
                我把这段过程固化下来。
              </Paragraph>
            </div>
            <Row gutter={[16, 16]}>
              {pillars.map((item) => (
                <Col key={item.title} xs={24} sm={12} lg={6}>
                  <Card variant="outlined" className="card">
                    <span className="icon">{item.icon}</span>
                    <Title level={3}>{item.title}</Title>
                    <Paragraph>{item.body}</Paragraph>
                  </Card>
                </Col>
              ))}
            </Row>
          </div>
        </section>

        <section id="flow">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>一次交付的路径</Title>
              <Paragraph>调度、校验、构建、签名、合并，每一步都留痕。</Paragraph>
            </div>
            <Steps
              direction="vertical"
              items={[
                {
                  title: "派单与认领",
                  description:
                    "调度器把就绪任务派给空闲 worker，worker 认领后拿到任务绑定的短时 token。",
                },
                {
                  title: "受控调用工具",
                  description:
                    "worker 调 shell_run / gate_run / workspace_write_file；工具按工作项校验写面与平台，越界直接 403。",
                },
                {
                  title: "真实构建与门禁",
                  description:
                    "门禁在同一执行画像里跑真实构建，产物 SHA-256 入链，生成可验证的溯源记录。",
                },
                {
                  title: "签名回执",
                  description:
                    "提交候选 ref，签名服务用 Ed25519 签发回执，观察者把事件追加进哈希链。",
                },
                {
                  title: "单车道合并",
                  description:
                    "合并者一次只合一个，合并后在同一环境复验，失败可回滚。",
                },
              ]}
            />
          </div>
        </section>

        <section id="profiles" className="alt">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>执行画像</Title>
              <Paragraph>
                任务必须声明平台，系统路由到对应执行体。跨平台的白名单之外的调用一律拒绝。
              </Paragraph>
            </div>
            <Table
              rowKey="key"
              pagination={false}
              dataSource={profiles}
              columns={[
                { title: "平台", dataIndex: "platform", width: 140 },
                { title: "载体", dataIndex: "carrier", width: 220 },
                { title: "用途", dataIndex: "use" },
              ]}
            />
          </div>
        </section>

        <section id="provenance">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>构建溯源</Title>
              <Paragraph>
                每次产出物的门禁都会自动留下一条可验证记录，不需要开发者额外填写。
                记录带指纹和签名，并写入审计链。
              </Paragraph>
            </div>
            <Row gutter={[24, 24]}>
              <Col xs={24} lg={12}>
                <dl className="kv">
                  <dt>产物指纹</dt>
                  <dd>门禁收集产物时计算 SHA-256</dd>
                  <dt>源码版本</dt>
                  <dd>构建时工作区的 HEAD</dd>
                  <dt>构建命令</dt>
                  <dd>门禁实际执行的 argv</dd>
                  <dt>构建环境</dt>
                  <dd>执行画像、虚拟机、内核与运行时</dd>
                  <dt>依赖清单</dt>
                  <dd>从工作区的 requirements / package.json / build.gradle 读取</dd>
                  <dt>构建人与时间</dt>
                  <dd>认领任务的 agent 与记录时间</dd>
                </dl>
                <Divider />
                <Paragraph>
                  验证会重算三件事：记录指纹、签名、产物文件的 SHA-256。替换产物或改动任意字段，
                  验证页立即变红并指出是哪一项不通过。
                </Paragraph>
              </Col>
              <Col xs={24} lg={12}>
                <pre>
{`curl -H "Authorization: Bearer $TOKEN" \\
  https://<host>/v1/provenance/B-20260101-EXAMPLE

curl https://<host>/verify/B-20260101-EXAMPLE
curl https://<host>/verify/B-20260101-EXAMPLE/qr.svg

`}<span className="c">{`# 藏在产物旁的标签包`}</span>{`
GET /v1/provenance/<build_id>/stamp.zip
  ├── provenance.json   记录 + 指纹 + 验证地址
  ├── verify-qr.svg     矢量二维码
  ├── verify-qr.png     位图，贴标用
  └── README.txt        怎么核对`}</pre>
              </Col>
            </Row>
          </div>
        </section>

        <section id="fabric" className="alt">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>联盟链锚定（可选）</Title>
              <Paragraph>
                哈希链是本地真相源；开启 Fabric 后，记录指纹会再提交到联盟链，由多方共同持有，
                单方无法悄悄改写历史。
              </Paragraph>
            </div>
            <Row gutter={[24, 24]}>
              <Col xs={24} lg={12}>
                <pre>{`MACP API ──HTTP──▶ fabric-bridge (:9310)
                        │
                        ▼
              peer0.org1 ──▶ orderer ──▶ 账本
                        │
                        └── 读回校验，返回 tx_id`}</pre>
              </Col>
              <Col xs={24} lg={12}>
                <Timeline
                  items={[
                    { children: "API 只认一个本地 bridge 地址：没有 SDK、gRPC、MSP 材料进入应用容器" },
                    { children: "链码 anchor：同编号同指纹幂等，异指纹直接拒绝" },
                    { children: "返回 tx_id 落库，公开验证页显示「联盟链交易」" },
                    { children: "链不可达时降级为「未锚定」，绝不影响构建记录" },
                  ]}
                />
                <Alert
                  type="info"
                  showIcon
                  message="可选项"
                  description="不配置 [fabric] 时系统完全照常运行；单组织网络用于验证链路，多机构联盟只需在 configtx 里加组织。"
                />
              </Col>
            </Row>
          </div>
        </section>

        <section id="start">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>五分钟跑起来</Title>
              <Paragraph>
                默认单机、只监听 loopback、不连任何外部设备。内置 mock 模型服务，
                不用真实 API key 也能看到「派单 → 写码 → 门禁 → 回执 → 合并」。
              </Paragraph>
            </div>
            <Row gutter={[24, 24]}>
              <Col xs={24} lg={14}>
                <pre>{`git clone https://github.com/k2dev-hub/macp.git
cd macp

`}<span className="c">{`# 生成配置、密钥、TLS，并自检`}</span>{`
./deploy/bootstrap.sh

docker compose -f deploy/docker-compose.yml up -d --build

`}<span className="c">{`# 打开 https://localhost:9443/
# 管理员 token 首次启动时生成（0600，不入库）`}</span></pre>
              </Col>
              <Col xs={24} lg={10}>
                <Card variant="outlined">
                  <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                    <div>
                      <Text strong>发布门禁</Text>
                      <Paragraph style={{ marginBottom: 0 }}>
                        <code className="inline">./deploy/release-check.sh</code> 会跑真实构建、
                        平台路由、构建溯源、联盟链与浏览器 E2E。
                      </Paragraph>
                    </div>
                    <div>
                      <Text strong>配置</Text>
                      <Paragraph style={{ marginBottom: 0 }}>
                        所有地址与外部服务都在 <code className="inline">config/macp.toml</code>，
                        <code className="inline">doctor</code> 只探测已启用的集成。
                      </Paragraph>
                    </div>
                    <div>
                      <Text strong>许可</Text>
                      <Paragraph style={{ marginBottom: 0 }}>MIT，可商用、可修改、可分发。</Paragraph>
                    </div>
                  </Space>
                </Card>
              </Col>
            </Row>
          </div>
        </section>

        <section className="alt">
          <div className="wrap">
            <div className="section-head">
              <Title level={2}>继续了解</Title>
            </div>
            <Row gutter={[16, 16]}>
              {[
                {
                  icon: <DeploymentUnitOutlined />,
                  title: "架构与文档",
                  body: "配置、MCP 网关、联盟链锚定的完整说明都在仓库里。",
                  href: `${REPO}/tree/main/docs`,
                  cta: "打开文档",
                },
                {
                  icon: <CloudServerOutlined />,
                  title: "部署形态",
                  body: "单机 compose 起步，也可以把应用与数据库拆到两台机器。",
                  href: `${REPO}#readme`,
                  cta: "看部署说明",
                },
                {
                  icon: <ThunderboltOutlined />,
                  title: "参与进来",
                  body: "MIT 许可，欢迎 issue 与 PR；CI 会跑配置解析、编译、脚本语法与前端构建。",
                  href: REPO,
                  cta: "去 GitHub",
                },
                {
                  icon: <BuildOutlined />,
                  title: "构建溯源",
                  body: "每个产物都有可验证的来源记录，扫码即可核对。",
                  href: `${REPO}/blob/main/docs/CONFIG.md`,
                  cta: "读配置文档",
                },
              ].map((item) => (
                <Col key={item.title} xs={24} sm={12} lg={6}>
                  <Card variant="outlined" className="card">
                    <span className="icon">{item.icon}</span>
                    <Title level={3}>{item.title}</Title>
                    <Paragraph>{item.body}</Paragraph>
                    <a href={item.href}>{item.cta} →</a>
                  </Card>
                </Col>
              ))}
            </Row>
          </div>
        </section>
      </main>

      <footer>
        <div className="wrap">
          <span>MACP — Multi-Agent Coordination Plane · MIT</span>
          <Space size="large">
            <a href={REPO}>GitHub</a>
            <a href={`${REPO}/blob/main/LICENSE`}>LICENSE</a>
            <a href="#top">回到顶部</a>
          </Space>
        </div>
      </footer>
    </>
  );
}
