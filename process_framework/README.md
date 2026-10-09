# 二连轧规则优先工艺框架（V0.1 草案）

本目录把“标准和主任确认的工艺规则先限定可行空间，DAT 只在空间内提供证据”落实成可运行的框架。它不修改、替换或自动下发当前 v0.4.1 推荐模型。

## 当前组成

- `rules_v1.json`：标准来源、规则优先级、字段单位、硬/软规则、数据分级办法和待确认事项。
- `material_routes_v1.json`：牌号、状态、退火状态机、设备限制和影子路线默认值。
- `spec_support_envelope_v1.json`：三输入规格组合支持点及影子拒绝/降置信阈值；不含任何历史输出。
- `process_owner_approval_checklist_v1.md`：主任、设备及质量三方需签认的工程参数清单。
- `outputs/<任务ID>/二连轧工艺框架_主任填写模板_v1.xlsx`：主任/设备/质量离线填写工作簿；不增加主操网页输入。
- `backend/app/process_framework.py`：单位张力换算、DAT A/B/C/D 审查、多轮轧制及退火路线状态机。
- `backend/app/route_governance.py`：真实卷/道次连续性、退火状态、累计冷作和训练资格校验。
- `scripts/audit_dat_framework_v1.py`：批量审查现有高速稳定段。
- `scripts/build_spec_support_envelope_v1.py`：仅从 A 类候选生成规格组合覆盖度影子闸门。
- `scripts/plan_route_v1.py`：生成影子路线候选；工程限制未批准时拒绝生产模式。
- `scripts/validate_route_records_v1.py`：校验主任确认的真实卷/道次CSV，输出逐路线阻断原因。

## 数据分级语义

- A：单轮黄金候选。当前仍需主任确认，且不代表路线元数据完整。
- B：需要人工复核；第一版不进入核心参数训练。
- C：操作或质量异常；只保留给风险/拒绝模型。
- D：核心数据损坏或缺失；完全排除。

是否属于 A 与是否可以训练是两道不同闸门：

1. 通过自动工艺审查；
2. 主任确认样本及其牌号、状态、道次、退火和公差口径；
3. 规则库状态改为 `approved_for_model_training`；
4. 才允许进入参数模型。

当前统计得到 555 个高速稳定生产段候选，`is_true_coil_or_pass=false`，且没有牌号、状态、真实道次和退火关联，因此可用于单道参数推荐和历史覆盖统计，但不能用于“轧几次/何时退火”的监督训练。

## 主任资料的结构化录入与校验

填写工作簿时只编辑蓝色区域。`真实卷道次` 一行代表一次通过 F1+F2 的二连轧道次；这不是给主操现场填写的页面。填写完成后：

1. 将 `道次CSV导出` 工作表单独另存为 CSV；
2. 如记录中出现 `FULL` 或 `PARTIAL` 退火，再将 `退火CSV导出` 单独另存为 CSV；
3. 执行：

```bash
python3 cold-rolling-system/scripts/validate_route_records_v1.py \
  --input /路径/道次CSV导出.csv \
  --anneal-recipes /路径/退火CSV导出.csv
```

校验器会分别报告：结构是否连续、标签是否完整、DAT是否全为A类、是否存在异常操作、最终质量是否合格、主任是否批准，以及规则库/设备/材料路线是否获准。只有全部通过才会得到 `route_training_eligible=true`；它仍不会自动启动训练或写PLC。

## 规格组合覆盖度闸门

旧 v0.4 只检查每个输入是否分别落在历史最小/最大值内，无法识别“每项都见过、三项组合却没见过”的情况。V1 新增组合支持度：

```bash
python3 cold-rolling-system/scripts/build_spec_support_envelope_v1.py
python3 cold-rolling-system/scripts/evaluate_rule_gated_v4.py
```

距离使用 `log(入口厚度)、log(出口厚度)、宽度、log(入口/出口)`，尺度来自 A 类唯一规格点的 P90-P10。历史输出不进入距离计算，也不参与推荐值加权。当前它仍是影子闸门：下一批真正未查看的新日期冻结验证、主任批准后，才允许接入页面的正式拒绝逻辑。

## 运行现有数据审查

在工作区根目录执行：

```bash
python3 cold-rolling-system/scripts/audit_dat_framework_v1.py
```

输出在 `outputs/process_framework_v1/`：

- `segment_audit.csv`：全量审查账本及原因；
- `gold_candidates_unapproved.csv`：未批准的 A 类候选；
- `risk_candidates.csv`：C 类异常/风险候选；
- `process_owner_review_template.csv`：给主任补充牌号、状态、退火、真实道次和审批意见；
- `audit_summary.json`、`audit_report.md`：机器及人工汇总；
- `manifest.json`：输入、规则和冻结模型哈希。

## 运行多轮影子规划

例如从 6 mm 轧至 0.7 mm：

```bash
python3 cold-rolling-system/scripts/plan_route_v1.py \
  --entry 6 \
  --exit 0.7 \
  --width 1213 \
  --alloy 1060 \
  --temper O \
  --anneal-state fully_annealed
```

影子模式会给出若干轧制次数和每次 `入口 → F1中间 → F2出口`，但会明确列出未确认的设备、材料和退火限制。

生产模式当前必须拒绝：

```bash
python3 cold-rolling-system/scripts/plan_route_v1.py \
  --entry 6 --exit 0.7 --width 1213 \
  --alloy 1060 --temper O --anneal-state fully_annealed \
  --mode production
```

只有设备限制、材料路线及退火规则全部确认后，生产模式才可生成“仍需人工确认”的候选；任何模式都不会写 PLC。

## 必须补齐的工程资料

第一优先级：

- 车间实际使用的牌号和状态清单；
- 各牌号完整/部分退火前后的状态转换；
- 每个状态允许的累计冷作真应变或主任采用的等价判断规则；
- F1/F2、整道次压下率限制；
- 左右侧轧制力、电机电流/扭矩/功率限制；
- 各厚度段单位张力上下限；
- 产品/客户厚度公差表。

第二优先级：

- 高速稳定生产段推荐辊缝的现场采用值、调整次数和最终厚度反馈；
- 版型单位、操作/传动侧方向及测量时延；
- 卷/道次与 DAT 文件的关联表。

没有这些信息时，框架可以筛选单轮稳定段和生成影子路线，但不能声称已经正确预测正式轧制次数或退火位置。
