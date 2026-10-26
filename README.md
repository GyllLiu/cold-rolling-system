# 冷二联工艺规划 v0.7.2

本仓库保存推荐算法、拍照采集页面、OCR 后端、模型参数、测试和部署脚本。现场照片、DAT 原始文件、生产 SQLite、OSS 对象、密钥和构建产物不入库。克隆后需自行配置云凭证；线上凭证只保存在 ECS 的 `/etc/cold-rolling/capture.env`。

## 当前线上功能

- 推荐页输入入口厚度、出口厚度、宽度、牌号和状态，返回道次、逐道厚度、张力、轧制力和辊缝等建议。
- `/capture` 拍照页支持拍照预览与保存、选择第 1–5 道、开始和结束；历史按北京时间日期查看照片、OCR 三字段及原始识别结果。
- 历史中可选地复核批号、牌号、状态及实际建模牌号，保留复核事件。OCR 存疑或未复核不会阻止拍照、保存、开始或结束。
- 当前 ECS 使用 SQLite 保存采集记录、OSS 存照片、百炼 Qwen-OCR 提取字段。最新部署与回退路径见 [`deploy/README.md`](deploy/README.md)。

## 克隆与开发

需要 Node.js 22.13+ 和 Python 3。安装前端依赖后运行 `npm run dev`；后端依赖见 `backend/requirements.txt` 与 `backend/requirements-capture-oss.txt`。部署所需环境变量名称在 `deploy/capture-cloud.env.example`，不要把真实 `.env`、数据库或照片提交到 Git。

```bash
# 首次克隆后创建本地构建配置；已有配置时不要覆盖。
cp -n .openai/hosting.example.json .openai/hosting.json
npm ci
npm run dev
```

原始 DAT 未随仓库上传，因此重新训练或重建依赖原始数据的模型产物时，需在本地另行提供数据；现有运行所需的模型 JSON 已包含在 `backend/app/model_artifacts/`。

拍照页面的本地 API 代理指向 `127.0.0.1:8001`，在另一个终端启动拍照后端：

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-capture-oss.txt
.venv/bin/python -m uvicorn backend.app.capture:app --host 127.0.0.1 --port 8001
```

默认本地 SQLite 和照片保存在忽略目录 `data/capture/`；未配置云凭证时可保存照片，但不进行云端 OCR。云环境变量参考 `deploy/capture-cloud.env.example`，实际凭证不随仓库分发。

## 最新离线训练与页面优化方案（2026-10-26）

- [前端界面优化方案](docs/frontend-ux-plan.md)：供前端开发或其他 AI 接手，属于待实施方案。
- [参数训练计划与实验结果](docs/pass-parameter-training-plan.md)：第 10 节为本轮 DAT 重训及同期 OCR 配对试验；新模型未发布至线上。
- `scripts/prepare_parameter_experiment_v073.py`：读取 DAT、提取高速生产候选段。
- `scripts/train_parameter_experiment_v073.py`：按日期验证参数模型，保存冻结配置、模型和逐条对比。
- `scripts/train_capture_pilot_v073.py`：对同日、同时间窗 OCR 配对样本做独立的小样本校准实验。

用户已确认“1050标1060”的实际牌号为 **1050**。最新离线标签规则保留原文并解析为 1050，不推广到其他未确认写法；原始实验保留旧规则用于复现。线上推荐模型的材料支持范围未因此自动扩展。

DAT 解析器 `parse_ibapda.py` 与数据基础构建器 `build_data_foundation.py` 已包含在仓库根目录。原始 DAT 和派生训练数据需另行提供，历史脚本默认从仓库上级的 `Data/`、`outputs/` 读取，复现目录结构及命令见训练计划。

训练使用独立环境，不修改采集服务依赖：

```bash
python3 -m venv .venv-training
.venv-training/bin/pip install -r scripts/requirements-training.txt
.venv-training/bin/python -m unittest discover -s tests -p 'test_parameter_experiment_v073.py' -v
```

现场照片、CSV 导出、原始 DAT、实验模型 `.joblib` 和训练输出不入库；仓库内已发布的模型 JSON 保持原版本。

输入牌号、来料状态、来料厚度、目标厚度、产品宽度和目标状态，输出：

- 推荐轧制道次与逐道厚度路线，必要时同时提供少道次方案
- 中间厚度、三段张力、F1/F2辊缝
- F1/F2传动侧、操作侧及合计轧制力
- 连续轧制范围、成品退火提示与高速历史运行范围

每次生成方案会自动保存本机试用记录；页面可导出CSV，以生成时间、牌号、退火状态和逐道参数与后续DAT生产段对齐。

训练与阈值统计只采用高速稳定双机架生产段。历史候选是相似工况拼接，不代表同一卷的真实多道轨迹。

## 厂家回复后的道次规则

道次规则版本为V0.7.2，DAT参数模型仍为V0.6.1，物理负荷校核沿用V0.7.1。

- 硬态相邻道次按F1对F1、F2对F2约降5个百分点；单道完成时两架相差5个百分点，豁免末道偏好限制。
- O态只有首道F1按软料安排，经过F1后按加工硬化料分配；物理模型继续累计变形，不把F1出口直接认定为H18。
- 厚度总体范围0.1～7.0 mm按线性三等分，分界2.4和4.7 mm。末道偏好随厚度连续变化，不再将旧38%/35%当作硬拒绝线。
- 3003首道F1绝不超过45%；硬态可用压下范围相对O态降低10%～20%，1100相对1060降低5%～10%。材料差异结合文献强度曲线计算，宽度进入物理负荷筛选。
- 首道50%不再作为通用硬限制。负荷只用于超限筛选，不参与候选排序；能少一道但末道更重时，两套路线同时显示并写入CSV。
- 本阶段不规划中间退火。F1实测力为0且F2为正的单轧记录、无法确认两架工作的记录及明确中间退火记录，均不进入双机架学习。原始记录保留。

可调整的试算设置集中在 `backend/app/model_artifacts/factory_route_policy_v072.json`：逐道差值暂用3～7个百分点；末道目标和容差采用连续插值；1060 O参考搜索上限保留60%，再按材料与状态缩减。这些是工程试算设置，不是厂家逐点确认的质量或安全边界。材料强度到可用压下范围的映射是工程敏感性近似，不能将文献强度值当作通用轧制极限。

材料来源：[ASM Handbook Volume 2 第49页表8及加工硬化讨论](https://materialsdata.nist.gov/bitstream/handle/11115/168/Properties%20of%20Aluminum%20Mill%20Products.pdf?isAllowed=y&sequence=3)。厚度范围扩展只影响路线覆盖；完整参数仍受已有DAT范围限制，0.1 mm等未覆盖规格不生成轧制力、张力和辊缝。

重新生成双机架历史视图并检查数据范围：

```bash
python3 scripts/build_tandem_history_v072.py
python3 tests/test_tandem_filter.py
python3 golden_data/tests/test_golden_cleaner.py
```

## 本地运行

```bash
npm install
npm run dev
```

访问 `http://127.0.0.1:3000`。

## 验证

```bash
npm run lint
npm test
cd backend && ../.venv/bin/python -m pytest -q
```

算法与工程基线报告：

- `../outputs/v6_high_speed/engineering_release_report_v6.md`
- `../outputs/v6_1_material_state/validation_report.md`

## 重新生成 v0.6.1

```bash
python3 scripts/train_compare_high_speed_v6.py
python3 scripts/build_engineering_baseline_v6.py
./.venv/bin/python scripts/build_validate_material_state_v061.py
```

当前运行产物：

- `backend/app/model_artifacts/route_model_v6.json`
- `backend/app/model_artifacts/route_model_v061.json`
- `backend/app/model_artifacts/material_state_v061.json`
- `backend/app/model_artifacts/engineering_baseline_v6.json`
- `backend/app/model_artifacts/recommendation_direct_v041.json`
- `backend/app/model_artifacts/equipment_physics_v071.json`
- `backend/app/model_artifacts/factory_route_policy_v072.json`
- `backend/app/model_artifacts/route_history_tandem_v072.json`
- `backend/app/model_artifacts/tandem_data_audit_v072.json`

冻结回滚产物 `recommendation_direct_v4.json` 保留，不覆盖。
