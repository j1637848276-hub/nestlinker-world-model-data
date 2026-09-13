# 2011–2024 审核后价格基线回填

本轮发布的是保守的同年度价格基线，不是全部合同历史的重建。
沿用旧快照的筛选口径，新增 TXT/CSV 适配、文件年度台账支持、审核哈希绑定和数据守恒门禁。
不修改已有快照、不自动去重、不纳入 2025 异常 ZIP。

## 准入与去向

- 原始 ZIP 与下载台账的 SHA-256、字节数必须一致。
- 每年必须有完整审核报告；报告绑定相同文件、相同年度、相同字节数。
  缺失、隔离、待适配或未完整审核的文件拒绝发布。
- `needs_review` 不是自动批准：本轮以明确的同年度筛选规则处理年度语义问题。
  合同年不同于文件年，或受理年不同于合同年，均不进入本轮价格回放视图。
  这是视图筛选，不是认定迟报无效；这些行继续保存在受控原始 ZIP。
- 无效日期、缺失/负金额、未知租赁类型、缺失分组字段不进入聚合，不填零、不猜测。
- 同一字段内容重复不证明同一交易。本轮保留来源出现次数；超过既有 1% 系统性重复门槛的文件仍整体隔离。
  2024 那一条额外精确重复没有擅自删除，计数称为来源行数，不能称为唯一成交数。
- 每个文件只贡献其自身合同年度，因此不同文件的已选 cohort 互斥，避免完整字段跨文件重叠重复计入。
  这不能识别同年修订或同一交易的不同字段版本。
- 仅发布合同月 × 区 × 建筑用途 × 租赁类型的数量及金额分位数；少于 10 行的组抑制。
  地址、地号、楼名、楼层、合同日明细不公开。
- 发布前验证：源行数 = 年度筛选 + 受理年筛选 + 无效行 + 小组抑制行 + 已发布组代表行数。

## 复现

先运行只读审核，输出目录必须未使用：

```powershell
$years = 2011..2024
python -m worldmodel_data audit-seoul-history --raw-dir data/raw/seoul-rental-files/2026-09-05 --acquisition-ledger data/acquisitions/2026-09-05/seoul-rental-files.json --output-dir data/work/seoul-history-audit/<new-run> --years $years
```

审阅报告后，从已经提交转换代码的干净工作区执行：

```powershell
python -m worldmodel_data publish-seoul-history --raw-dir data/raw/seoul-rental-files/2026-09-05 --acquisition-ledger data/acquisitions/2026-09-05/seoul-rental-files.json --audit-dir data/work/seoul-history-audit/<reviewed-run> --snapshot-date <new-date> --years $years
python -m worldmodel_data validate
```

输出的新快照不可覆盖。manifest 记录审核文件哈希、源文件哈希、真实获取时间、转换 commit 和明确的视图限制。
pre-2022 回填没有审核报告会拒绝执行。公开 Git 只包含聚合快照与汇总说明，原始记录和审核细分报告留在忽略目录。

## 不可推断

受理年度只有年度粒度，并非申报日期。不得据此声称恢复每个历史日的真实 as-of 数据。
迟报筛选可能改变年底价格分布，跨年覆盖不证明完整或无选择偏差。
本视图只适用于区月级历史价格基线，不提供押金返还结局、当前房源、个体安全评分或大模型训练结果。
