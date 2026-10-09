# 冷二联黄金工艺数据清洗

运行：

```bash
cd "/Users/garryliu/Documents/Data Anylisis/cold-rolling-system/golden_data"
python3 main.py
```

程序读取工作区中全部 `outputs/data_foundation*` 文件索引和候选生产段，再从原始 DAT 直接按 1 Hz 重采样。它不会修改或覆盖原始 DAT。

主要输出位于 `../../outputs/golden_recipe_v1/`：

- `golden_recipe_dataset.csv`
- `abnormal_segments.csv`
- `all_high_speed_window_audit.csv`
- `low_speed_window_audit.csv`
- `all_channel_inventory.csv`（全部347个原始通道的名称、类型和覆盖率）
- `column_profile.csv`
- `column_mapping.md`
- `calibrated_thresholds.json`
- `cleaning_report.md`
- `figures/*.svg`

限制：没有真实卷号、道次、牌号、退火和质量标签时，GOLD_A只表示“过程稳定且无已知异常”，不等于已经证明成品合格。
