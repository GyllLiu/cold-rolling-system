# 冷二联首设参数直接预测

单一用途的冷二联首设参数网页。现场只输入：

- 入口厚度
- 目标出口厚度
- 来料宽度

系统使用冻结的正则化多项式回归方程，直接预测11项机器首设参数，并给出2项轧制力校核。网页推理时不检索历史卷、不做近邻加权平均。

## 当前模型

- 训练数据：260712—260718，共281个高速稳定段
- 封存测试：260719，共64个高速稳定段
- 13个预测目标中11个优于历史近邻基线
- 低速主操调试区不作为训练目标
- 系统只给建议，不写入PLC

完整验证报告位于项目上级目录的 `outputs/recommendation_model_v3_report.md`。

## 本地运行

首次准备：

```bash
npm install
npm run build
```

启动：

```bash
./scripts/start-local.sh
```

浏览器访问 `http://127.0.0.1:3000`。计算接口已经包含在同一个网站中，不再依赖本机FastAPI服务。

## 重新训练

先在项目上级目录生成七天数据底座：

```bash
python3 build_data_foundation.py 260712 --output-dir outputs/data_foundation_260712
python3 build_data_foundation.py 260713 --output-dir outputs/data_foundation_260713
python3 build_data_foundation.py 260714 --output-dir outputs/data_foundation_260714
python3 build_data_foundation.py 260716 --output-dir outputs/data_foundation_260716
python3 build_data_foundation.py 260717 --output-dir outputs/data_foundation_260717
python3 build_data_foundation.py 260718 --output-dir outputs/data_foundation_260718
python3 build_data_foundation.py 260719 --output-dir outputs/data_foundation_260719
```

再在本项目目录运行：

```bash
python3 scripts/train_validate_direct_prediction.py
npm test
```
