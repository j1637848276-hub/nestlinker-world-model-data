# NestLinker World Model Data

韩国外国租客决策世界模型的数据底座。这个仓库优先解决四件事：来源可追溯、采集可复现、时间口径明确、模型不会把“事实 / 估计 / 教学模拟”混在一起。

## 当前覆盖

截至 2026-09-03，目录登记 34 个公开来源；两个可复核快照包含 7 个数据文件、17,060 条发布记录。覆盖数量、限制与下一批采集顺序见 `docs/COVERAGE.md`。

- 租赁成交：国土交通部 RTMS，按住宅类型、区、月份采集。
- 人口与外国人：行政安全部居民人口、法务部登记/居所外国人统计。
- 合法经营与建筑核验：持牌中介、建筑物台账、考试院消防登记的来源契约。
- 风险基线：HUG 保证事故与返还保证、租赁价格指数。
- 行动约束：大学、地铁、通勤、外国人登记及租赁法律指引。
- 环境上下文：洪涝、空气与必要生活设施等候选数据。

`catalog/datasets.json` 是全量数据源目录。`data/snapshots/` 只保存已经通过许可、隐私和质量门禁的快照。目录中访问方式为 `api_key` 或状态为 `manual_review` 的来源不会因为“能下载”就自动进入模型。

“全量目录”是对当前调查范围的可扩展登记，不代表已穷尽互联网；新增来源必须保留发现日期、官方落地页和使用限制。

## 快速使用

年度历史文件回填前，先运行新增的 `audit-seoul-history`。下载、运行、报告字段和退出码见 [首尔年度审计说明](docs/SEOUL_HISTORY_AUDIT.md)。审计不会自动去重或发布快照。

```bash
python3 -m unittest discover -s tests -v
python3 -m worldmodel_data validate
python3 -m worldmodel_data catalog --category market
```

首尔租赁历史回放使用官方年度文件。原始 ZIP 保存在 gitignore 的 `data/raw/`，聚合时删除地址、地号、楼名和楼层：

```bash
python3 -m worldmodel_data publish-seoul-history \
  --raw-dir data/raw/seoul-rental-files \
  --acquisition-ledger data/acquisitions/2026-09-03/seoul-rental-files.json \
  --snapshot-date 2026-09-03 \
  --years 2022 2023 2024

python3 -m worldmodel_data historical-replay \
  --snapshot-dir data/snapshots/2026-09-03/seoul-rental-history \
  --output docs/research/historical-replay-results.json \
  --minimum-counts 10 30 100
```

复核受理年过滤的年末选择偏差（输出为不可覆盖的机器可读产物）：

```bash
python3 -m worldmodel_data receipt-filter-sensitivity \
  --raw-dir data/raw/seoul-rental-files \
  --acquisition-ledger data/acquisitions/2026-09-03/seoul-rental-files.json \
  --output docs/research/receipt-filter-sensitivity.json \
  --years 2022 2023 2024 \
  --minimum-count 30
```

回放只检验区级历史价格带的稳定性，不检验实时房源、个体合同安全、押金能否返还或外国租客摩擦。

运行最小世界模型的固定情景矩阵：

```bash
python3 -m worldmodel_data minimum-world-model \
  --snapshot-dir data/snapshots/2026-09-03/seoul-rental-history \
  --scenario-file docs/model/MINIMUM_WORLD_MODEL_SCENARIOS_V0.json \
  --output /tmp/minimum-world-model-v0.json
```

已发布的参考输出见 `docs/model/MINIMUM_WORLD_MODEL_RUN_V0.json`。该模型只用于验证机制门槛、行动顺序和参数单调性；完整 `safetyGate` 固定为 `unknown_missing_outcome_calibration`。`affordabilityRate` 与 `depositExposureExceedanceRate` 是历史聚合价格带上的合成压力测试，不是当前找房成功率或押金损失概率。

仓库保存可校验的聚合快照与机器结果；年度原始 ZIP 因包含不必要的物业明细而不提交 Git。manifest 固定其哈希，但官方文件可能更新，因此新的 checkout 可以复跑已发布聚合上的回放，未必能重新取得字节完全相同的原始 ZIP。异常文件证据保存在 `data/quarantine/`。

从 NestLinker 主仓导入已有的公开数据派生快照：

```bash
python3 -m worldmodel_data import-nestlinker \
  --source-root ../nestlinker-source \
  --snapshot-date 2026-09-01
```

拉取 RTMS 原始数据需要在本地设置 `DATA_GO_KR_SERVICE_KEY`：

```bash
export DATA_GO_KR_SERVICE_KEY='your-decoding-key'
python3 -m worldmodel_data fetch-rtms --months 3 --seoul-only
```

RTMS 原始响应写入 gitignore 的 `data/raw/`；经过最小化、去标识和字段标准化后，才能发布到 `data/snapshots/`。

## 数据契约

每个快照必须有 `manifest.json`，至少记录：

- `source_id` 与官方落地页；
- 抓取时间、数据时点和地理范围；
- 原始或派生状态；
- 文件 SHA-256、字节数和记录数；
- 适用限制及不得做出的推断。

世界模型消费数据时必须输出 `observed | modeled | synthetic` 标签；本仓库只提供 `observed` 和明确标记的 `derived_observed` 数据，不保存模型生成概率。

## 许可

仓库代码使用 MIT。数据不随代码重新授权，各文件继续受原始提供者条款及韩国公共著作物许可约束，详见 `catalog/datasets.json` 和快照 manifest。
