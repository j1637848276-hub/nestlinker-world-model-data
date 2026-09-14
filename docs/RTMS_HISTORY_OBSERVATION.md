# API 历史回填与低频复查

近期线仍使用 `config/rtms-observation.json` 和 `data/raw/rtms-observations`，每周采集最近 12 个合同月份加研究固定队列，不修改已有版本。

历史线使用单独的 `data/raw/rtms-history-observations`。首次工程范围为 2011-01 至 2025-09，按年分批；接口逐区逐月查询，2011 不是已证实的统一数据起始边界。每个分区仍须分页完成；合法零条不是证明该地区当年没有交易。这个范围是可调整的回填计划，不是已经成功采集的声明。

## 创建历史计划

```powershell
python scripts/prepare_rtms_history.py --output-dir data/work/rtms-history-plan/initial
```

生成 15 个逐年配置，最后一年只含 1–9 月。输出目录不可覆盖，文件 LF，无密钥。`rolling_contract_months=0` 禁用近期滚动窗口；固定队列非空，避免创建空成功批次。使用现有示例的首尔 25 区和三个已启用接口，不自动启用第四个住宅来源。

先运行最新历史年份，再依次运行更早年份，逐批检查退出码和 manifest，失败时停止后续批次以避免反复无效请求：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_rtms_observation.ps1 `
  -Config data/work/rtms-history-plan/initial/rtms-history-2025.json `
  -StorageRoot data/raw/rtms-history-observations
```

恢复未完整批次使用已有 `observe-rtms --storage-root ... --run-id ... --resume`，密钥安全注入环境；已 finalized 的版本不能恢复或覆盖。存储根目录互斥锁防止并发写入。历史和近期独立存储，不影响近期审计的运行分母与连续观察目标。

## 低频复查与审核

### 初次全计划分批控制器

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_rtms_history.ps1 `
  -PlanDir data/work/rtms-history-plan/2026-09-14 `
  -StorageRoot data/raw/rtms-history-observations -RequestBudgetPerApi 6000
```

控制器按年份从新到旧执行，哈希口径一致且已完整、审计通过的基线自动跳过；未完成批次恢复原 run_id。2025 已有基线仍位于原目录，更早年份存于 `by-year/<year>`，逐年审计，避免把全部年份的指纹同时加载入内存。运行状态、实际请求尝试次数、活动年份保存在 `control/executions`；控制器互斥锁阻止重复启动，采集器自身存储锁仍保留。

预算默认每个 API 每次控制器执行最多 6,000 次请求尝试（包括重试），不是官方日额度或账户剩余额度。已存在批次与其他电脑请求不在此预算内；不得自动反复重启绕过预算。门户列出的开发申请流量为 10,000，具体批准额度和剩余额度仍应在账户核对：[公寓接口说明](https://www.data.go.kr/data/15126474/openapi.do)。遇到 HTTP 401/403/429、来源错误或预算耗尽，控制器立即停止，保留已取得的分区和脱敏错误证据；未结束 run 不计成功。普通网络错误仍沿用采集器有上限重试，分区失败后不启动下一年份。

每个年份完成后生成本地审计；完成采集但标准化失败也停止后续年份。控制器 completed 仅表示配置计划已取得合格版本，不表示接口全历史已穷尽。计划之外年份、非首尔地区和未经批准的新接口不自动扩展。

初次回填审核后，建议每月再次执行同一历史年份配置，生成新 run_id；也可根据配额降低为季度、分年轮转。频率只是工程选择，不能保证覆盖所有修订。年度分批执行不声称是同一时刻的全历史快照。

历史配置 `cadence_days=31` 是月度诊断间隔，不是已部署调度，也不精确表达日历月。若实际采用季度或轮转，不能以这个参数声称每月连续达标；应单独报告实际执行日期和被查询年份。多年度混合存储的运行完整率仅表示批次执行完整，不表示每个月份拥有相同观察覆盖。版本比较须逐分区核查查询范围和 schema。

```powershell
python -m worldmodel_data audit-rtms-versions `
  --storage-root data/raw/rtms-history-observations `
  --output-dir data/work/rtms-history-audit/first-review --cadence-days 31
```

首次历史内容均为 baseline；今天查到旧合同，不恢复旧时点的数据可见性。API 和年度文件保持独立来源：核对合同月份、行政区、住宅类型、申报/受理口径、筛选与抑制差异，不逐行覆盖，不相加为唯一交易数，不将差异自动归咎于来源错误。跨来源价格聚合发布与口径对照报告另设准入步骤，本次采集代码不自动发布。

## 运行条件

## 本机标准化明细整理

完整且审计通过的版本可导出为 UTF-8/LF `records.jsonl`，只允许输出到 `data/raw` 或 `data/work`，目录不可覆盖：

```powershell
python -m worldmodel_data export-rtms-observation `
  --storage-root data/raw/rtms-history-observations --run-id 20260914T102730Z `
  --output-dir data/work/rtms-normalized/2025-baseline
```

导出逐页核对原始对象哈希、逐分区行数和运行总行数。完成后写 manifest，固定明细哈希、字节数、输入清单哈希、导出器与标准化器源码哈希，以及来源/月度记录数和可选字段缺失数。中途失败没有完整 manifest，不能当作合格导出。

金额仍以万韩元保存，显式零与缺失分开。独栋/多户 `totalFloorAr` 存为 `reported_area_sqm`，面积口径标记 `source_totalFloorAr`，不填入 `exclusive_area_sqm`。楼层、期限、申报时间等可选缺失保持 null。`first_seen_at` 暂不编译，保持 null 并明确状态，应通过可比较版本审计编译；不能用导出日期或合同日期代填。来源原始字段仍在 XML 中保留，标准化不是删除原始版本。

这些是本机操作明细，不通过公开快照准入；不得上传 GitHub 或公开 CI。首次回填控制器负责采集和逐年审计；明细导出是完成后独立只读步骤，不修改正在运行的批次。

原始明细、指纹和计划保存于 Git 忽略目录，不上传公开 CI。当前凭据与持久磁盘在本机可用。2026-09-14 已将现有月度补查计划更新为独立历史线，仅查询 2025-01 至 2025-09，下一次计划首尔时间 2026-10-01 04:00；已有近期每周计划保持不变，下一次为 2026-09-21 03:00。更早年份尚未部署低频复查，也没有完成全部初次回填。

运行要求持续在线主机与备份；本地计划要求电脑开机且应用运行，依据 [OpenAI Docs 定时任务说明](https://learn.chatgpt.com/docs/automations?surface=app) 核对。计划更新不代表未来已执行成功。Windows 命令的 Bypass 仅作用于该次进程，不修改系统执行策略。
