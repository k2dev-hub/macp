# Fabric 锚定（联盟链兼容层）

MACP 的哈希链是本地真相源；Fabric 负责把**指纹**交给多方共同持有，使单方无法悄悄改写历史。
这一层是可选件：不配置 `[fabric]` 时系统完全照常运行。

## 结构

```
MACP API ──HTTP──▶ fabric-bridge (:9310) ──peer CLI──▶ peer0.org1 ──▶ orderer ──▶ 账本
                        │
                        └── 持有管理员身份、读回校验、返回 tx_id
```

- API 容器里没有 Fabric SDK、gRPC 和 MSP 材料，只认一个 HTTP 地址；
- bridge 与 peer 同机，负责签名调用与读回校验；
- 链码 `provenance` 每个 build_id 一个键，同指纹重复提交幂等，异指纹提交被拒。

## 演示与升级

- 课设演示剧本：[docs/FABRIC-DEMO.md](../../docs/FABRIC-DEMO.md)
- 一键演示：`./demo.sh`（`--auto` 连播，`--skip-build` 只看账本）
- 链码升级：`./upgrade-cc.sh`（自动递增 sequence，`recent`/`stats` 需要 1.1+）
- 一致性巡检：`python3 ./verify-ledger.py`

## 一次性准备

```bash
docker pull hyperledger/fabric-tools:2.5 hyperledger/fabric-peer:2.5 \
             hyperledger/fabric-orderer:2.5 hyperledger/fabric-ccenv:2.5
cd deploy/fabric && ./up.sh          # 生成 crypto、建通道、装并提交链码
```

`up.sh` 只用镜像里的工具，不在宿主机安装 Fabric 二进制。

## 开机后

```bash
systemctl enable --now macp-fabric-bridge
```

`/etc/macp-fabric-bridge.env` 里放 `FABRIC_BRIDGE_TOKEN=...`，与
`deploy/secrets/fabric_bridge_token` 一致；接口与 `config/macp.toml` 的 `[fabric]`
段对应。

## 验证

```bash
curl -s http://172.28.0.1:9310/health
curl -s -H "Authorization: Bearer $(cat deploy/secrets/fabric_bridge_token)" \
     http://172.28.0.1:9310/anchor/B-20261116-0E8A63
```

MACP 侧：

```bash
curl -H "Authorization: Bearer $TOKEN" https://<host>/v1/provenance/<build_id>/chain-anchor
```

返回 `record.fingerprint` 与本地记录一致即视为锚定成功；`fabric_tx_id` 落库后，
公开验证页会显示“联盟链交易”。

## 注意

- 这是**单组织开发网络**（1 orderer + 1 peer）。论文里的多机构联盟需要再各加一套
  org 的 peer 与 crypto，`configtx.yaml` 里加 Org2/Org3 并更新通道策略即可。
- 链码依赖在 `up.sh` 里用宿主机 Go 一次性 `go mod vendor`（`vendor/` 不入库），
  之后 peer 在 `fabric-ccenv` 里离线构建；升级链码时改 `--version/--sequence` 并重新 commit。
- 账本数据在 volume `peer0`，`./up.sh --reset` 会清空。
