# 粮仓熏蒸隔离放行采证台

该离线命令行工具把粮批仓位与虫样、施药许可与封仓、独立通风复测与出库分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。迟到高虫样撤销旧放行；破封重置时钟，旧通风读数失效。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m grainrelease demo --db demo.sqlite
python -m grainrelease audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`lot-import`、`fumigate-import`、`release-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。时间字段采用同一时区的 ISO 字符串。

熏蒸区人员清空走独立名册：`occupants.inside` 按现场时间保留每张工牌的完整在场区间（借牌、陪同、重返都各自成段，不按上传顺序或 badge 集合去重），`person_capture` 把区间联接到真实人员，`evacuation.evaluate` 结合警报切点、集合点本人/见证人/轮次核验和公告账本给出 `complete`。`fixtures/evac-reentry.json` 的 `branches` 分别给出「双人见证借牌 + W1 重新集合」与「影像和本人确认 V1 再入」两路证据。人员清空结论只用于结束人员搜寻，不改变粮批放行、虫害处置、运单报送或仓位计划。

人员名册入口保持可读：

```python
import json
from grainrelease import evacuation
scenario = json.load(open('fixtures/evac-reentry.json', encoding='utf-8'))
result = evacuation.evaluate(scenario)           # 未追加证据：continue searching
result['complete']                                # False
result['announcements'][-1]['kind']               # 'revoke'
```

`evaluate` 同时接受 `custody`（双人见证借牌）、`identity_evidence`（影像 + 本人确认）、`statements`（口头说明，只扩候选不归属）、`accompaniment`（陪同入场，持牌人与被陪同人各自承担在场责任）与已发布 `announcements`。返回的 `announcements` 是只追加账本：已发布通知原样保留，矛盾证据生成指名 `revoke`，重新满足条件才生成新的 `clear`。`complete(gates, badges)` 仍兼容最初的工牌集合接口；工牌一旦有重返区间，扁平旧名单不再自动成立。完整 `evaluate` 要求集合点记录显式写出人员、见证人和警报轮次，缺一项即记入 `muster.rejected` 并保持搜寻。

