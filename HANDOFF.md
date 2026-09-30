# 交接文档（HANDOFF）

> 更新时间：2026-09-30。本文档面向下一个接手的开发者/AI 会话。
> 需求来源：`课堂复习Agent-简化需求文档.md`（以下简称「需求」）。

## 1. 当前状态：MVP 已可运行

项目已按需求完成第一版实现并通过端到端验证，**可以日常使用**，但有两类事项待办：
（a）只在真实课堂麦克风场景下才能验证的项目（见 §4）；（b）明确的未实现/简化项（见 §5）。

## 2. 环境与运行

| 项 | 状态 |
| --- | --- |
| Python | 3.13.15（需求写 3.11；全部依赖在 3.13 验证可用，faster-whisper 1.2.1 / ctranslate2 4.8.2） |
| Node | v22.23.2，前端已构建进 `frontend/dist`（由后端静态托管，单进程） |
| 启动 | 双击 `start.bat` 或 `.venv\Scripts\python.exe backend\run.py` → http://127.0.0.1:8000 |
| 数据目录 | 默认 `<仓库>/data`（已 gitignore）；用环境变量 `AXOL_DATA_DIR` 改到任意位置 |
| 网络注意 | GitHub 直连 HTTPS 在本机不稳（间歇重置）；首次曾用本机代理 7897 完成连接，之后 **push 地址已切换为 SSH**（`git@github.com:Crloxa/Axol-s_ClassNote.git`，端口 22 实测可用，备用 `ssh.github.com:443`）；首次转写下载 whisper 模型前设 `HF_ENDPOINT=https://hf-mirror.com` |

## 3. 已验证（对应需求 §9 MVP 验收）

- ✅ 创建课堂、导入 `.pptx`/PDF/DOCX/Markdown/代码，显示页码、标题、标题路径或行号（本地解析）
- ✅ 麦克风链路：前端 MediaRecorder(webm/opus) 分块上传 → 后端追加文件 → av 解码 → Silero VAD 切分 → faster-whisper 转写（增量 flush，带起止时间戳）；用 Windows TTS 生成真实语音实测，转写逐字正确
- ✅ 转写片段可编辑文字、标 ★ 重点、手动关联页码；页面标记（`M` 键/按钮）把标记间转写归属到页；未关联片段不会丢（进入「未关联转写」）
- ✅ 范围选择（全部/章节多选 + 资料勾选）控制本地整理与模型输入；单章生成不会自动塞入无映射资料
- ✅ 三种生成模式 + 本地规则整理（离线可用）；结果保留来源标注（`[PPT 第 N 页]`、`[文件：位置]`、`[课堂 mm:ss-mm:ss]`）与「待核对」
- ✅ 发送预览：服务商/端点/模型、章节页码、勾选资料、转写时间范围与字符数、token/费用估算；一次性确认令牌（15 分钟、用后即废、过期需重新预览）
- ✅ API Key 只进 Windows 凭据管理器（keyring），不入库/文件/日志；配置接口永不回显 Key
- ✅ 出站安全：模型端点仅允许 http/https 且解析为公网地址（拒绝内网/环回/保留）——安全约束，注意这意味着**不能**把端点指向本机 Ollama/LM Studio
- ✅ 结果可编辑、导出 Markdown/HTML；关闭「保留原始音频」后停止录音即删音频
- ✅ 冒烟测试 25 项全过：`.venv\Scripts\python backend\tests\smoke_api.py [speech.wav]`（需后端在跑）
- ✅ 浏览器实测：三栏布局、导入、转写流、预览→确认→结果编辑全流程无报错

## 4. 待真机验证（本会话无法做）

1. 真实麦克风录音的权限流程与音质下的转写效果（建议中文用 `base` 或 `small`，当前默认 `base`；测试时曾切 `tiny`）。
2. 长课程（1-2 小时）下的内存/延迟表现：flush 每次全量解码音频文件（解码廉价，但文件会持续增长）。
3. MediaRecorder 分块在真实浏览器断续网络下的追加完整性（自动化环境只能整段 wav 模拟）。
4. 真实 DeepSeek/OpenAI/Anthropic Key 的实际调用（无 Key，未实测；请求体格式按各家文档写）。

## 5. 未实现 / 简化项（按优先级）

1. **费用估算很粗**：tokens ≈ 字符数/1.7，仅 CJK 口径；价格只算输入侧。可引入 tiktoken。
2. **扫描版 PDF 无 OCR**（需求第一版明确不做，界面只提示「未提取文字」）。
3. **令牌不感知「范围外数据变化」**：预览后新产生的转写会被计入生成（范围定义不变）。若要严格化，可在令牌中存数据指纹并在生成时校验。
4. **PPT 重新导入会清空章节**：旧 slides 删除后，已有片段的 `manual_slide`/markers 变成悬空页码（生成时会被过滤，不报错）。可改为提示用户确认。
5. **无删除课堂/结果的接口**；无结果列表分页（单用户量级下暂不需要）。
6. **转写无「识别中」半成品状态**：只输出 VAD 认定完整的语句，状态列恒为 `final`（需求允许此简化，但字段已预留）。
7. **前端单文件 `App.tsx` 偏大**（约 700 行）：功能没问题，后续加功能时建议拆组件。
8. `on_event("startup")` 是 FastAPI 弃用写法，将来升级时改 lifespan。

## 6. 架构与文件地图

```
backend/
  app/
    config.py     数据目录、上传上限（AXOL_DATA_DIR 环境变量）
    db.py         SQLite schema + settings 助手（WAL，外键级联）
    parsers.py    PPTX/PDF/DOCX/MD/代码 → 结构化文本 + 来源位置（source_items 统一展开）
    stt.py        录音会话、音频追加、VAD+whisper 增量转写（全局锁，一次一段）
    providers.py  服务商注册表、keyring 凭据、SSRF 校验(validate_external_url)、模型调用
    generate.py   范围物化(materialize)、发送预览+令牌、本地规则整理、模型提示词(需求§8原文)
    api.py        全部 REST 端点（21 个，见 /api/docs 的 OpenAPI）
    main.py       装配：CORS(仅 5173 开发用) + 托管 frontend/dist
  run.py          uvicorn 只监听 127.0.0.1:8000
  tests/smoke_api.py  端到端冒烟（可选 wav 测转写）
frontend/src/
  App.tsx         三栏 UI 全部交互（录音器、快捷键 M、范围选择、预览弹窗、结果编辑）
  api.ts/types.ts fetch 封装与类型
data/             运行时数据（axol.db、lessons/<id>/ppt|sources|audio），已 gitignore
```

关键设计决定：
- **转写时间轴 = 音频文件内位置**（暂停期间无数据，不计入）；页面标记也存该时间轴，两者天然对齐。
- **转写增量**：flush 时全量解码（便宜）→ VAD 找完整语音段（后面有停顿才算完）→ 只转写新增段；`processed_end` 记进度，force（停止）时收尾并按设置删音频。
- **未关联转写只在「全部内容」范围进入生成**（需求 §5.3：不遗漏；§5.2：单章不被污染）。
- **webm 追加**：MediaRecorder timeslice 分块顺序追加即合法 webm 流，av 流式解码容忍尾部残缺 cluster。
- **出站模型请求的 SSRF 防护**（`providers.call_model`）：同函数内完成协议白名单 + DNS 解析 + 公网 IP 校验，
  然后**把连接目标直接设为校验过的 IP 字面量**（URL host = IP，`Host` 头与 TLS `sni_hostname` 扩展保留原主机名），
  结构上消除 DNS rebinding 窗口；`follow_redirects=False` 防重定向绕过。注意：因此端点**不能**指向本机/内网
  服务（如 Ollama/LM Studio），这是安全约束的刻意取舍。

## 7. 常用排查

- **启动报「前端未构建」**：`cd frontend && npm install && npm run build`。
- **转写报下载模型失败**：设 `HF_ENDPOINT=https://hf-mirror.com` 后重启，或先在设置里把模型切 `tiny`。
- **测试/脚本请求本机 API 失败但 curl 正常**：系统代理会劫持 httpx，加 `trust_env=False`（见 smoke_api.py）。
- **换数据盘**：`set AXOL_DATA_DIR=D:\axol-data` 后启动；SQLite 与文件目录会一起建过去。
- **git 推送失败**：HTTPS 直连 GitHub 不稳时改走 SSH（已配 `pushurl = git@github.com:Crloxa/Axol-s_ClassNote.git`）；
  SSH 22 端口被断时用 `ssh.github.com:443`（在 `~/.ssh/config` 加 `Host github.com\n  Hostname ssh.github.com\n  Port 443`）。
