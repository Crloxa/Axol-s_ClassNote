# 课堂复习 Agent（Axol's ClassNote）

把课堂实时转写、主 PPTX 和参考资料合并成可靠、可追溯复习资料的本机网页工具。
**全部数据只留在本机**：文件、音频、转写、结果都在你选择的数据目录里；只有你点「确认发送」后，
才会把**选中范围**的提取文本发给你自己配置的大模型 API。

## 快速开始

前置：Python ≥ 3.11（3.13 已验证）、Node.js ≥ 20。

```bat
:: 1. 后端依赖
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt

:: 2. 前端构建
cd frontend
npm install
npm run build
cd ..

:: 3. 启动（或直接双击 start.bat）
.venv\Scripts\python.exe backend\run.py
```

打开 <http://127.0.0.1:18471>（只监听本机回环地址）。

## 使用流程

1. **新建课堂** → 导入主 `.pptx`（自动提取每页标题/正文/页码）。
2. 按需添加 PDF / DOCX / Markdown / 代码参考资料（本地解析，扫描页 PDF 会提示未提取文字）。
3. 点 **开始记录**（此时才请求麦克风），课堂中翻页时按 `M` 或点「标记当前页」，
   把之后的转写归属到当前 PPT 页；转写条目可改字、标 ★ 重点、手动关联页码。
4. 选范围（全部内容 / 勾选章节 + 资料）→ 选模式（重点讲义 / PPT 外补充 / 详细提纲）→
   选引擎（本地规则整理，离线可用 / 远程模型）。
5. **生成预览** 确认字符数、token 与费用估算后 **确认发送**（每次生成都要重新确认）。
6. 结果可编辑，导出 Markdown / HTML。

### 模型配置

右侧选「远程模型」→ 选服务商（DeepSeek / OpenAI 兼容 / Anthropic 兼容）→ 填端点、模型名、
输入价格 → 保存；API Key 只存入 **Windows 凭据管理器**，不写任何文件。

### 离线语音模型

首次转写会从 HuggingFace 下载 whisper 模型（tiny 约 75MB）。网络受限时先设置镜像再启动：

```bat
set HF_ENDPOINT=https://hf-mirror.com
```

## 隐私与网络

- 录音/转写/文件/结果都在 `data\`（可用环境变量 `AXOL_DATA_DIR` 指定其他磁盘位置）。
- 转写默认本机离线运行（faster-whisper + Silero VAD），断网可完成课堂记录。
- 发送预览明确列出将发送的内容；取消则零模型请求。
- 出站模型请求强制校验：仅允许 http/https 且目标必须为公网地址（拒绝内网/环回/保留地址）。
- 设置里可关闭「保留原始音频」，转写完成后自动删除音频文件，只留文字。

## 开发

```bat
:: 后端（自动重载）
cd backend && ..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
:: 前端开发服务器（/api 代理到 18471）
cd frontend && npm run dev
```

端到端冒烟测试（需后端已启动；可选传入 wav 验证转写链路）：

```bat
.venv\Scripts\python backend\tests\smoke_api.py
```

当前进度与未完成事项见 [HANDOFF.md](HANDOFF.md)。
