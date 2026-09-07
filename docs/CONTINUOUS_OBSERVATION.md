# RTMS 连续版本采集与数据时效审计

## 目的与边界

本管道每周重新查询相同的合同月份，保存每次真实观察到的来源版本，用来描述新增、消失、内容变化、重复、缺失字段和采集完整性。查询过去 12 个月只是一次运行的回看范围；连续观察时长只按真实成功运行的时间计算。

它不生成房源安全评分、押金返还概率或官方迟报概率。`first_seen` 表示本系统第一次观察到内容，不等于来源首次公开日期。没有稳定官方交易 ID 时，完全字段指纹只表示相同内容，不能证明是同一笔交易。

## 存储结构

默认存储根目录是 Git 忽略的 `data/raw/rtms-observations/`：

```text
objects/sha256/<前两位>/<sha256>.xml   内容寻址的 RTMS 原始响应
objects/sha256/<前两位>/<sha256>.zip   内容寻址的年度文件
runs/<run_id>/run.json                  API 运行清单
runs/<run_id>/partitions/...            来源×地区×合同月份明细
annual-runs/<run_id>/run.json           年度 ZIP 的独立观察清单
```

相同响应字节只保存一份对象，但每次运行仍保存观察时间和对象引用。运行结束且全部分区完整后不可覆盖。同一个未完整运行可用同一 `run_id` 和 `--resume` 恢复；已经完整结束的运行拒绝恢复或覆盖。根目录锁阻止两个采集器同时写入。

原始响应、指纹及分区清单不能提交 Git。需要把整个存储根目录放在有备份的持久磁盘或对象存储同步层，不能依靠临时 CI runner 和短期 artifact。

## 配置

复制示例并按部署环境调整：

```powershell
Copy-Item config/rtms-observation.example.json config/rtms-observation.json
```

默认配置：

- 每 7 天运行一次；
- 最低连续观察目标为 3 个完整自然月，目标值为 6 个完整自然月；
- 每次查询最近 12 个合同月份；
- 查询首尔 25 个行政区；
- 只默认启用目录中已 `seeded` 的 apartment、officetel、single_multi；
- `fixed_contract_months` 每周持续观察指定月份，避免滚动窗口退出；
- `low_frequency_contract_months` 只在传入 `--include-low-frequency` 时加入，适合月度或季度补查。

固定队列必须结合研究期设置。例如开始运行时，可把研究关注的合同月份写入 `fixed_contract_months`，连续三至六个月都保持不变。不要把一次查询的 12 个月误写成 12 个月连续观测。

## 手动运行

API key 只通过进程环境变量读取：

```powershell
$env:DATA_GO_KR_SERVICE_KEY = "your-decoding-key"
python -m worldmodel_data observe-rtms `
  --config config/rtms-observation.json `
  --storage-root data/raw/rtms-observations
```

完整运行返回 0；创建了运行清单但存在不完整分区时返回 2。指定 `--run-id` 后，可以恢复未完整运行：

```powershell
python -m worldmodel_data observe-rtms `
  --storage-root data/raw/rtms-observations `
  --run-id 20260907T180000Z `
  --resume
```

低频补查应使用计划外的独立运行或第二个调度任务：

```powershell
python -m worldmodel_data observe-rtms `
  --config config/rtms-observation.json `
  --storage-root data/raw/rtms-observations `
  --include-low-frequency
```

旧的 `fetch-rtms` 命令保留兼容用途；连续观测应使用 `observe-rtms`。

## 分页与完整性

每个来源、行政区、合同月份形成一个分区。每页保存：页码、尝试次数、响应条数、来源 `totalCount`、响应对象 SHA-256 和内容 SHA-256。

以下情况使分区成为 `incomplete`：

- 重试上限后仍请求失败；
- `totalCount` 缺失或非法；
- 分页过程中 `totalCount` 改变；
- 在达到总数前得到空页；
- 不同页返回相同的非空内容；
- 最终条数与 `totalCount` 不同；
- 超过配置的最大页数。

合法的 `totalCount=0` 是完整空分区。分区不完整、滚动窗口退出或 schema 变化时，审计不会把未出现内容解释为交易撤销。

## 金额和标准化

显式金额 `0` 可以保留。缺失或无法解析的押金/月租不会转成 0，也不会因此被误判为全租；原始响应仍保存，标准化失败数和字段缺失数进入质量报告。

现有 `normalize_item` 的 ID 包含完整原始内容及 `occurrence_index`，只能稳定表示同一内容与出现次数，不能证明真实交易身份。版本审计使用 SHA-256 完整字段指纹及多重集合比较：

- `added_occurrences`：本次多出的完全内容出现次数；
- `removed_occurrences`：本次减少的完全内容出现次数；
- `possible_revision_candidates`：同一可比分区内新增数与减少数的较小值，只用于建立复核队列；
- schema 不同的比较标记为不可靠，不进入汇总变化量。

## 版本审计

输出目录必须不存在：

```powershell
python -m worldmodel_data audit-rtms-versions `
  --storage-root data/raw/rtms-observations `
  --output-dir data/work/rtms-version-audit/2026-09-07 `
  --cadence-days 7
```

报告包含真实运行次数、实际观察天数和完整自然月、三个月/六个月目标是否达成、估算漏跑、运行和分区完整率、字段缺失率、精确重复率、内容变化率、疑似修订复核数量，以及新增内容的 `first_seen` 时间区间。持续目标只有在达到完整自然月且期间没有估算漏跑时才通过。

四个状态分别表示：

- `collection_succeeded`：采集器完成并写下运行清单；
- `version_complete`：全部计划分区与来源总数一致；
- `audit_passed`：完整版本同时没有标准化失败记录；
- `public_release_eligible`：本层固定为 false，公开发布仍需聚合隐私和 snapshot 准入。

## 年度 ZIP 版本

年度文件不与 RTMS API 混合覆盖。下载完成后单独登记：

```powershell
python -m worldmodel_data observe-seoul-annual-files `
  --input-dir data/raw/seoul-rental-files/2026-09-05 `
  --storage-root data/raw/rtms-observations `
  --run-id annual-20260907
```

相同 ZIP 再次观察会复用内容对象，同时新增本次观察清单。这个命令只记录文件版本，不代替 `audit-seoul-history` 的行级质量审计。

## 定时部署

Windows 可让任务计划程序在服务账户下调用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\project\nestlinker-world-model-data\scripts\run_rtms_observation.ps1
```

任务应选择“无论用户是否登录都运行”，将 `DATA_GO_KR_SERVICE_KEY` 配置在该服务账户可读取的安全环境中，并确保 `data/raw/rtms-observations` 位于持久磁盘。任务退出码 2 或非零时应告警，不能将失败当作一次完整观察。

Linux 示例位于 `deploy/systemd/`。安装前必须替换工作目录、运行用户、配置文件和存储路径；timer 使用 Asia/Seoul 每周一 03:00，并启用 `Persistent=true` 以便机器恢复后补跑。补跑仍保留真实执行时间，不伪造原计划时间。

没有配置持久后端，因此本项目没有启用 GitHub Actions 定时采集。公共 fork 的 runner 和 artifact 不适合保存房屋级长期原始内容。

## 合成端到端演示

下面命令生成两个标记为 `synthetic` 的版本，第二个版本增加一条记录：

```powershell
python scripts/demo_rtms_versions.py --output-dir data/work/rtms-demo/first-run
```

演示报告会显示一次新增，但真实运行次数和真实观察跨度仍为 0。输出目录不可覆盖，合成内容不会进入公开 observed snapshot。
