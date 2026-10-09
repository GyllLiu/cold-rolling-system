# 阿里云 ECS 部署

## 当前发布：2026-10-09 OCR 复核

当前线上目录为 `/opt/cold-rolling/releases/20261009-ocr-review`，`/opt/cold-rolling/current` 指向该目录；SQLite 已迁移到 `user_version=4`。本版改进批号、牌号、状态三字段识别，对可疑结果做局部重试，并在历史页加入可选的人工复核及可追溯事件。复核不参与采集流程的准入判断，不阻止拍照、开始或结束。现场访问码按既有要求保持关闭。

生产 SQLite 和照片位于 `/var/lib/cold-rolling/capture/`，不在发布目录，也不提交到 GitHub。切换前一致性备份在 `/var/lib/cold-rolling/capture/backups/pre-release-20261009-ocr-review/`。发布后数据库完整性、两项服务、公网页面、历史详情与推荐接口均已验证。回退代码前应评估数据库 schema 兼容性，切勿覆盖生产记录。

## 2026-10-07 道次选择

拍照页开始前须选择第 1–5 道；后台保存 `selected_pass_no`（整数 1–5），历史、详情、当前进行中记录与 CSV 导出均显示该字段。它是现场人员选择的道次，与按卷号推算的 `pass_no`、按批号排序的 `batch_sequence` 分开保存。SQLite 升级至 `user_version=3`，既有记录保持 `selected_pass_no=NULL`，不按时间追填。开始请求重试时核对所选道次，避免同一请求悄悄改号。

生产发布目录为 `/opt/cold-rolling/releases/20261007-pass-selector`，发布前 SQLite 与本地照片备份在 `/var/lib/cold-rolling/capture/backups/pre-release-20261007-pass-selector/`。依赖仍引用 `20260906-0026`，不要清理该目录。阿里云 Workbench 的 OSS 中转上传超时，本次以 Workbench 远程命令分块传输 438KB 差量包并在 ECS 上验证 SHA-256 后发布。26 项拍照后端测试、前端类型检查、Lint 与构建通过；部署后检查数据库完整性、前后端服务和公网页面。未写入测试生产记录。

## 2026-09-23 照片转正与重拍（21:48 已上线）

主图、放大预览、历史缩略图和详情采用完整画面的竖向适配；横图默认旋转 90°，方向不正确时可手动旋转。已保存照片的展示角度写入 SQLite，不改写原文件、OSS 对象或 SHA256。新上传图片记录 EXIF 转正后的有效宽高，避免原生相机照片重复转向。

未开始照片可重拍：新照片通过校验并成功入库后，在同一事务里建立双向替换关系；失败保留旧照片有效。上传重试沿用同一个 photo_id、内容和替换目标。网络结果不确定时，页面禁止继续重拍，先重试确认；明确失败后可重拍或取消。已关联生产记录的照片禁止替换，已替换照片禁止开始或再次识别。历史默认仅显示有效照片，可勾选显示旧照片追溯；刷新后可从历史选择未开始照片继续操作。

数据迁移 user_version=2，仅新增 display_rotation、replaces_photo_id、replaced_by_photo_id 三列，不清理或合并既有数据。旧照片和 OCR 结果仍实际保留，不算另一条有效照片或生产记录；重拍本身不创建生产记录。产生替换记录后不可直接回退到不识别替换关系的旧后端，否则历史和开始校验会失去过滤保护。

用户确认后于北京时间 2026-09-23 21:48 发布到 `/opt/cold-rolling/releases/20260923-portrait-retake`，前后端均切换到该目录，依赖链接至 `20260906-0026`。保留 `20260916-1740` 及其后端 `/opt/cold-rolling/backend-releases/20260920-batch`，未改访问码/云配置。发布包补齐既有 capture_corrections.py，线上此前的批号修正能力保持不变。

发布前备份：`/var/lib/cold-rolling/capture/backups/pre-release-20260923-portrait-retake/`（一致性 SQLite、本地 photos 归档；OSS 原对象不变）。迁移前后逐表所有旧列和值与备份一致：84 照片、84 生产记录、84 最新 OCR、97 OCR 尝试、1 人工修正。24 后端测试、5 前端测试、TypeScript、ESLint、构建通过；服务器隔离迁移/重拍/旋转/重试验证和公网 smoke 均通过。生产数据只做读取和兼容性迁移，未创建测试生产记录，也未修改历史识别值。含生产批号与照片标识的数据审计报告仅在本地保留，不纳入源码仓库。

- 实例：`i-0xi9lpu1fhjgmkm4z61z`，地域 `us-east-1`，公网 `8.221.114.18`。
- 推荐页面：`https://8.221.114.18/`；拍照页面：`https://8.221.114.18/capture`。
- 程序：`/opt/cold-rolling/current`，发布目录在 `/opt/cold-rolling/releases/`。
- 照片与 SQLite：`/var/lib/cold-rolling/capture/`，与程序更新目录分开。
- 工位访问码：ECS 上 `/etc/cold-rolling/capture.env` 的 `CAPTURE_ACCESS_CODE`。这是应用访问码，不是阿里云 AK。
- 服务：`cold-rolling-web`（127.0.0.1:3100）、`cold-rolling-capture`（127.0.0.1:8101）、`nginx`（80/443）。
- `cold-rolling-cert-renew.timer` 每小时检查证书续期。公网 IP 证书有效期较短，需要保持此定时器运行及 80 端口可访问。

首次安装：本地构建测试后执行 `python3 deploy/build-release.py`，通过 Workbench 上传到独立的发布目录，执行 `deploy/install-ecs.sh`；公网验证通过后执行 `deploy/setup-https.sh`。

验证：`python3 deploy/check-public.py`；服务器查看 `systemctl status cold-rolling-web cold-rolling-capture nginx` 和 `journalctl -u cold-rolling-capture`。

后续更新先比较 `package-lock.json` 和 `backend/requirements-capture.txt`。依赖未变时，新发布目录可链接旧发布目录的 `node_modules`、`.venv`，避免在小规格 ECS 上重复安装完整开发依赖；被引用的旧目录必须保留。依赖变化时需先准备对应运行环境。对数据库创建一致性备份后，再切换 `current` 并重启两个 cold-rolling 服务；保留旧发布目录用于回退。不要删除数据目录或覆盖访问码文件。

当前为联网采集，主操作区保留拍照、开始、结束三个按钮，无需填写牌号、状态和卷号。拍照后先预览，可重拍或保存；下方历史按北京时间日期筛选，默认当天、拍摄时间倒序，可查看大图、识别结果与原文。开始记录上机时间，结束记录整道完成时间，中途停机不拆分。

SQLite 保存照片、道次记录、最新 OCR 结果与每次识别返回。OCR 和 OSS 适配器已实现，需配置云资源后才能真正启用，未配置时页面明确显示待开通。识别结果保持“未核对”，不自动把批号当卷号或把卡片状态当来料状态。卷号未确认前不计算道次。资源与数据库详见 [拍照数据库与云资源](拍照数据库与云资源.md)。

增量迁移会给旧 `photos` 表增加列。首次更新到本版前，应将待保留回退版本中的 `INSERT INTO photos VALUES(...)` 改为带原五列显式列名的 INSERT，避免迁移后回退版本写入失败。先备份数据库，再切换服务；新旧程序依赖未变时沿用原环境。

安装 `deploy/cold-rolling-backup.service` 和 `.timer` 到 `/etc/systemd/system/` 并启用定时器，每天北京时间 03:20 执行一致性备份。本机保留 14 份，OSS 已配置时同步异机副本。

数据暂存 ECS 本机。备份或迁移时，对 SQLite 使用 `.backup`/Python `Connection.backup()` 创建一致性副本，并复制 `photos/`，不要只拷贝运行中的 `records.sqlite3` 主文件。

## 2026-09-13 云服务配置

运行模型为 `qwen-vl-ocr-2025-11-20`，提取提示词版本 `production-card-v2`。样张实测可提取批号、牌号和卡片状态，手写公差及备注仍需核对，结果不自动作为已确认训练标签。

实际 OSS Bucket 为北京地域 `cn-beijing`，ECS 位于弗吉尼亚，因此使用 `https://oss-cn-beijing.aliyuncs.com` 公网 Endpoint。实际凭证保存在受保护的 `capture.env`，不要使用同地域内网地址。原图、缩略图和数据库备份保持私有，通过短时签名查看照片。

## 三字段采集口径

OCR 提示词版本 `production-card-v3-core` 只提取 `batch_no`、`alloy`、`card_state`，保留提供方原始返回作为审计依据。历史页只展示批号、牌号和状态；公差、尺寸、投入量及工艺备注不参与字段提取。

记录/CSV 导出增加 `batch_no`、`batch_sequence`、`ocr_alloy`、`card_state`、`ocr_status`、`ocr_prompt_version` 和设备标识。`batch_sequence` 是同设备同批号按开始时间排序的记录顺序，供 DAT 关联使用；不将批号改写成卷号，也不把此顺序直接当作已验证的物理道次。分析程序应结合时间、实际厚度连续性与同批多卷情况确认关联。

状态保存卡片原值，作为训练分组标签；不自动覆盖原有 `incoming_state`。`succeeded` 表示字段通过当前格式检查，不等于人工核实。训练集应保留 OCR 版本与质量标记，模糊或冲突样本不可直接用作可靠标签。当前版本尚未接入 DAT 自动匹配及每日训练任务。

2026-09-14 拍照界面更新：主屏保留拍照、开始、结束三个主要按钮；未保存照片时禁止开始。当前照片新增批号、牌号、状态只读识别结果及后台状态提示，识别不阻塞开始/结束。历史卡片突出三项字段，详情保留原图、拍照和起止时间、核对提示与重新识别。人工修改/确认接口尚未实现，识别成功不代表人工核对。前端发布目录为 `/opt/cold-rolling/releases/20260914-2107`，沿用上一版后端与依赖目录；不得清理其引用的旧发布目录。工位访问码已按用户要求在服务器配置更新，本文档不记录密码。

2026-09-16 保存耗时排查：ECS 已为 `/api/capture/` 启用 `/var/log/nginx/cold-rolling-capture-timing.log` JSON 日志，包含 total_s、upstream_s、connect_s、header_s、请求和响应字节数、request_id；不记录 Cookie、密码、请求体或查询参数。保持原访问日志，Nginx 配置检查通过后平滑重载。总耗时与 upstream_s 的差额包含请求上传、客户端响应传输等，不能全部视作上传耗时；拍照时间到落库时间也包含人工预览，不能当作上传耗时。用当日 179169 字节 / 1920×1080 照片在隔离临时目录运行现有保存函数三次约 62.8/35.7/36.2 ms，仅用于服务器计算基线，不包含网络或并发负载。未修改生产照片和 OCR 队列，未更改保存逻辑。

2026-09-16 保存期间整页不可操作修复：旧预览遮罩在上传期间禁用了所有按钮和 Esc，已改为点击保存立即关闭预览，在主页面显示上传进度；100% 仅代表请求已发送，须收到服务器确认才能开始。照片复用本页 Blob 预览，避免保存后重下原图；45 秒超时保留本页照片并允许用相同 photo_id/内容重试；未确认保存时关闭页面触发浏览器离开提示。历史组件在进度更新时不重复渲染。TypeScript、ESLint、两项上传事务测试通过；隔离浏览器测试故意延迟响应约 20 秒，确认等待中能打开/关闭预览并操作历史，确认后才开放开始。仅发布前端，目录 `/opt/cold-rolling/releases/20260916-1731`，后端继续引用上一发布版本，不能清理被引用的旧目录。没有用现场数据创建测试生产记录。

2026-09-16 摄像头竖向取景与免访问码：前端发布 `/opt/cold-rolling/releases/20260916-1740`。横向摄像头默认旋转 90°，拍摄界面提供旋转按钮，四个方向按浏览器记忆；采用完整画面等比适配，并对保存 JPEG 同步旋转，不裁切也不镜像。原生竖向摄像头默认不旋转。摄像头真实视野不因页面旋转扩大。几何边界与上传事务共四项测试、TypeScript、ESLint、构建通过；隔离浏览器用四色虚拟摄像头验证旋转，保存得到 1080×1920 JPEG，四角像素正确。用户明确要求暂时取消工位访问码，ECS `CAPTURE_ACCESS_CODE` 已清空，拍照/历史/照片/导出可直接访问，OSS Bucket 仍保持私有。公网检查使用 `python3 deploy/check-public.py --public-capture`；以后重新启用访问码时不带该参数。数据未改动，生产后端继续引用旧发布目录，仍需保留依赖链。
