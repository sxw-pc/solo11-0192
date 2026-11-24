# 粮仓熏蒸隔离放行采证台

该离线命令行工具把粮批仓位与虫样、施药许可与封仓、独立通风复测与出库分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。迟到高虫样撤销旧放行；破封重置时钟，旧通风读数失效。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m grainrelease demo --db demo.sqlite
python -m grainrelease audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`lot-import`、`fumigate-import`、`release-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。时间字段采用同一时区的 ISO 字符串。

## 离线多页运单报送

出车前运单页（运单/装载/封签）经 `grainrelease.manifest` 逐页签名采录：每页带编号与版本，整份运单以全部页摘要算出的 **内容根** 标识，任一页改版即换根。`grainrelease.filing_capture` 用 SQLite 记账发送轮次（每轮一个监管提交令牌、一个内容根和重试水位游标）、网关包头确认与监管端 HMAC 回执，回执按编号幂等去重、逐页计数。

`grainrelease.filing.status` 的闭合口径同时满足三条硬约束：

1. **三页同版** —— 回执内容根、页版本、页摘要必须与当前原件逐页相符；编号凑齐 1/2/3 但混入上一版内容仍为 `pending`；
2. **网关包头不等于受理** —— 只有带监管端签名的回执（或同根全页汇总回执）计入，网关 `gateway_acks` 仅留痕；
3. **重试不混版** —— 断网重试开启新令牌轮次，仅补同一内容根下未受理的页，旧版已送出页与公告全部保留可追溯。

监管端两种经批准的恢复操作：`record_summary_receipt` 凭同根全页汇总回执补录并保持受理；`reopen_with_partial_proof` 凭部分到达证明撤销旧 `accepted` 公告、保持 `pending` 并开新轮次补缺页。`fixtures/manifest-mixed-version.json` 与 `tests/test_filing_recovery.py` 覆盖半包、混页、断网重试、两路恢复及旧公告回读。

