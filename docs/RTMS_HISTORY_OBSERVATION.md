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
powershell.exe -NoProfile -File scripts/run_rtms_observation.ps1 `
  -Config data/work/rtms-history-plan/initial/rtms-history-2025.json `
  -StorageRoot data/raw/rtms-history-observations
```

恢复未完整批次使用已有 `observe-rtms --storage-root ... --run-id ... --resume`，密钥安全注入环境；已 finalized 的版本不能恢复或覆盖。存储根目录互斥锁防止并发写入。历史和近期独立存储，不影响近期审计的运行分母与连续观察目标。

## 低频复查与审核

初次回填审核后，建议每月再次执行同一历史年份配置，生成新 run_id；也可根据配额降低为季度、分年轮转。频率只是工程选择，不能保证覆盖所有修订。年度分批执行不声称是同一时刻的全历史快照。

历史配置 `cadence_days=31` 是月度诊断间隔，不是已部署调度，也不精确表达日历月。若实际采用季度或轮转，不能以这个参数声称每月连续达标；应单独报告实际执行日期和被查询年份。多年度混合存储的运行完整率仅表示批次执行完整，不表示每个月份拥有相同观察覆盖。版本比较须逐分区核查查询范围和 schema。

```powershell
python -m worldmodel_data audit-rtms-versions `
  --storage-root data/raw/rtms-history-observations `
  --output-dir data/work/rtms-history-audit/first-review --cadence-days 31
```

首次历史内容均为 baseline；今天查到旧合同，不恢复旧时点的数据可见性。API 和年度文件保持独立来源：核对合同月份、行政区、住宅类型、申报/受理口径、筛选与抑制差异，不逐行覆盖，不相加为唯一交易数，不将差异自动归咎于来源错误。跨来源价格聚合发布与口径对照报告另设准入步骤，本次采集代码不自动发布。

## 运行条件

原始明细、指纹和计划保存于 Git 忽略目录，不上传公开 CI。当前凭据与持久磁盘在本机可用，但历史月度调度尚须按验证后的配额、在线主机和备份条件部署；不能把配置文件生成称为调度已启动。已有近期每周计划保持不变。
