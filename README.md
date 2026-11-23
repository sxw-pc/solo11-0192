# 粮仓熏蒸隔离放行采证台

该离线命令行工具把粮批仓位与虫样、施药许可与封仓、独立通风复测与出库分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。迟到高虫样撤销旧放行；破封重置时钟，旧通风读数失效。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m grainrelease demo --db demo.sqlite
python -m grainrelease audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`lot-import`、`fumigate-import`、`release-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。时间字段采用同一时区的 ISO 字符串。
