# AI CleanCut 后端

FastAPI + SQLAlchemy + SQLite，FFmpeg + NumPy 处理音频。REST 响应通过 Pydantic 转为 camelCase。

## 运行与验证

一键启动前后端：在项目根目录执行 `./start.sh`，或双击根目录的 `启动 AI CleanCut.command`。按 `Ctrl+C` 停止，日志位于根目录 `.run/`。

在 backend 目录、已激活的 Python 虚拟环境中运行：

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8001
pytest -q
```

系统需安装 `ffmpeg`。可选 `python tests/browser_smoke.py` 运行真实浏览器测试，需要 Python Playwright 及 Chromium；浏览器脚本自行启动临时服务，不使用真实数据库。

## 结构

- `app/routers/`：会话、增强、复核和 WebSocket 协议。
- `app/models/`、`app/schemas/`：持久化模型和接口结构。
- `app/services/transcription.py`：本机 faster-whisper、后台任务、词级时间戳和原子发布。
- `app/services/rendering.py`：按已接受的删除区间生成 PCM WAV，保存渲染快照并校验并发修改。
- `app/services/audio_analysis.py`：解码、质量分析、谱减法降噪和声学相似度。
- `app/services/stub_pipeline.py`：停止后分析、增强任务、候选复核与实时质量检测。文件保留历史名称，随机语义信号已移除。
- `app/ws/connection_manager.py`：同一会话只接收一个录制端，通知失败不影响后台分析。
- `tests/`：音频、REST、流水线、WebSocket 和回归测试。

## 一致性约定

- 原始音频停止后不再接受追加。增强结果命名为 `denoised_{job_id}.wav`，每个版本对应独立文件。
- 增强任务按 `created_at_ns` 查询最新，前端进度查询使用明确的 job ID。启动时自动兼容旧 SQLite 表，旧任务按已有插入顺序回填。
- `resultVersionId` 指向本次结果，`errorMessage` 提供失败原因。
- 无法解码时标记 `FAILED`，不提供伪造的质量报告、版本或保护指标。
- 人声保护未通过仍可保留候选结果供试听，但不更新 `RENDER_SOURCE` / `EXPORT_SOURCE`。
- 计算和 FFmpeg 调用通过线程执行，避免在异步请求循环中等待重计算；数据库 Session 不在线程间共享。
- 重复候选复核跳过已处理信号，同一进程内串行化；保留人工接受/拒绝结果，不从候选文字伪造转写。
- 后台任务不具备进程重启恢复能力，按单 worker 运行。

测试通过替换 `app.db.SessionLocal` / `app.db.engine` / `app.config.AUDIO_DIR` 隔离数据。服务内部使用模块属性访问这些可替换对象，不在导入时复制绑定。`isolated_backend` 同时把 `transcription.dependency_available()` 打成 `False`，避免终态化触发的自动转写在测试机上真的加载 faster-whisper 模型（本次开发环境已装该依赖）；需要验证自动转写触发本身的测试自行在用例内重新打开。

## API

```text
GET   /api/health
POST  /api/sessions
GET   /api/sessions
GET   /api/sessions/{id}
PATCH /api/sessions/{id}
DELETE /api/sessions/{id}
POST  /api/sessions/{id}/stop
POST  /api/sessions/{id}/import
GET   /api/sessions/{id}/versions
GET   /api/sessions/{id}/versions/{vid}/audio
GET   /api/sessions/{id}/roles
GET   /api/sessions/{id}/quality
GET   /api/sessions/{id}/waveform?buckets=200
GET   /api/sessions/{id}/warnings
POST  /api/sessions/{id}/enhancement
GET   /api/sessions/{id}/enhancement
GET   /api/sessions/{id}/enhancement/{job_id}
POST  /api/sessions/{id}/decisions
GET   /api/sessions/{id}/decisions
PATCH /api/sessions/{id}/decisions/{did}
POST  /api/sessions/{id}/decisions/confirm
POST  /api/sessions/{id}/render
GET   /api/sessions/{id}/render
POST  /api/sessions/{id}/transcription
GET   /api/sessions/{id}/transcription
GET   /api/sessions/{id}/transcription/{job_id}
GET   /api/sessions/{id}/transcript
GET   /api/sessions/{id}/transcript/export?transcriptionJobId=asr_...&format=txt
POST  /api/sessions/{id}/decisions/suggest
POST  /api/sessions/{id}/decisions/reject-suggestions
WS    /ws/recording/{session_id}
```

`waveform` 的 buckets 范围为 1–2000。增强模式支持 `auto/conservative/aggressive`，分别映射到 `balanced/light/strong`；只执行谱减法降噪，`off` 不执行降噪。确认接口只保存选择，渲染通过独立的 `POST /render` 执行。转写接口读取当前完整转写，旧候选记录通过 `jobId=null` 区分。

## 导入已有音频

`capture_mode=batch` 的会话可以跳过实时录制，直接导入一个已有音频/视频文件。`POST /import` 接受 `multipart/form-data` 的 `file` 字段，写入后复用与 WS 停止录制完全相同的终态化流程（`on_capture_stop`）：解码、质量分析、生成原始版本与全部角色分配。会话必须是 `capture_mode=batch` 且 `capture_status=IDLE`（未导入过），否则返回 409；单次导入上限 300MB，超出返回 413，空文件返回 422。

原始文件按内容嗅探解码，不依赖文件名后缀（与 WS 录制把 webm/ogg/mp4 统一存成 `raw.webm` 是同一套逻辑），所以可以直接导入 wav/mp3/m4a 等常见格式。导入前用 `ffprobe` 读取真实采样率与声道数用于展示，探测失败不影响导入，真正的可用性判断交给解码本身；`mimeType` 取自上传请求的 `Content-Type`，用于回放时正确设置响应头。解码失败与录制失败走同一套语义：整个请求仍返回 201，但 `liveAnalysisStatus=FAILED`，不生成质量报告或版本，不是 HTTP 错误。

导入完成后的会话与实时录制终态化后完全等价，可以直接触发增强、转写、手动剪辑和渲染。

**抢占状态与原子发布**：真正开始接收上传前，先在写事务里把 `capture_status` 从 `IDLE` 原子切到 `IMPORTING` 再提交（不会占着写锁等整个上传完成）；两个并发的导入请求只有一个能抢到，另一个立即拿到 409，不会同时写同一份 `raw.webm`。同一时间只允许 `capture_mode=batch` 之外的会话建立 WS 录制连接——批处理会话（含 `IMPORTING` 窗口）一律拒绝 WS 连接，避免被误连的实时录制抢写导入目标文件。上传内容先写到 `raw.webm.importing` 临时文件，全部收到且大小/非空校验通过后才用 `os.replace` 原子改名成 `raw.webm`；上传失败或抛异常会清理临时文件、把 `capture_status` 退回 `IDLE` 允许重试，`raw.webm` 本身不会停在半写状态。

## 修改与删除会话

`PATCH /api/sessions/{id}` 改标题和/或项目，两个字段都可选，传空字符串（去空格后）返回 422；不改音频、决策或任何处理状态。

`DELETE /api/sessions/{id}` 彻底删除：手动清空所有关联表（`AudioVersion`、`RoleAssignment`、`AudioQualityReport`、`EditDecision`、`EnhancementJob`/`VoiceProtectionMetric`、`Warning`、`LiveSignal`/`QualityEvent`、`TranscriptToken`、`TranscriptionJob`、`RenderOutput`）再删会话行本身，并 `shutil.rmtree` 整个会话的音频目录。没有配置外键级联、SQLite 也没开外键约束，这一步不手动做就会留下孤儿数据。不可撤销，前端在删除前有一次二次确认。

**活动任务期间禁止删除**：`capture_status` 为 `RECORDING`/`PAUSED`/`IMPORTING`，或 `live_analysis_status=FINALIZING`，或存在活动中的增强任务（`QUEUED`/`PROCESSING`/`QUALITY_CHECKING`）/转写任务（`QUEUED`/`LOADING_MODEL`/`TRANSCRIBING`）时一律拒绝（409）。这些任务持有 `session_id` 引用、会在完成时继续写库或读写磁盘文件；此时删除会在任务收尾时插出孤儿数据，或读取已被 `rmtree` 的文件报错。检查和级联删除在同一个写事务里完成，避免检查通过后、真正删除前又有新任务插入的竞态窗口。

## 本机录后转写

```bash
source .venv/bin/activate
pip install -r requirements-asr.txt
python scripts/prepare_asr.py
```

默认多语言 `base` 模型在 CPU 上以 int8 推理。`CLEANCUT_ASR_MODEL` 可设模型名或已转换的本地模型目录；`CLEANCUT_ASR_MODEL_DIR` 设置下载缓存，默认 `data/models`。缓存准备完成后可用 `CLEANCUT_ASR_OFFLINE=1` 禁止模型联网下载。没有云端音频上传。

faster-whisper 的多语言模型对中文默认偏向繁体输出，实测无论 `language=auto` 还是显式传 `zh` 都一样，不是语种识别错了，是模型出字的字形习惯问题。`info.language == "zh"` 时用 `zhconv` 转成简体再入库，转写文字最终看到的是简体中文；这一步只转字形，不影响时间戳。

`on_capture_stop` 终态化成功（录制停止或导入完成）后会自动排队一次转写（`requested_language=auto`），不需要先调用这个接口；`stub_pipeline._auto_start_transcription` 检测到 `dependency_available()` 为假（没装 `faster-whisper`）时静默跳过，不阻塞终态化本身，也不产生警告。`POST /transcription` 仍然保留，用于切换语言重新识别，或者自动排队被跳过后手动重试：请求 `{"language":"auto"}`（也支持 `zh`、`en`），返回 202 和任务 ID。同会话已有任务返回 409，缺少可选依赖返回 503。任务状态为 `QUEUED → LOADING_MODEL → TRANSCRIBING → COMPLETED/FAILED`，前端按任务 ID 查询，进度按已转写音频位置估算，不包含下载进度。

转写固定使用完整时间轴的 `ASR_SOURCE`，不会把剪短的成片当输入。CPU 模型在后台线程执行，并串行调度以控制内存；成功时一次性替换该会话的转写词，失败不发布部分文字，不修改人工剪辑决策。启动时将上次中断的任务标为失败，用户可以重试，不会自动恢复推理。

`transcript_tokens` 自动兼容增加 `end_ms` 和 `job_id`，API 为 `endMs`、`jobId`。历史候选这两个字段为空，不能当作真实词级时间戳。新增 `transcription_jobs` 表保存模型、源版本、语言和状态。

验证：`pytest tests/test_transcription.py -q` 检查状态和原子性；`.venv/bin/python tests/asr_smoke.py` 用 macOS 合成的中英文音频跑真实模型、词级区间及剪辑渲染。`python tests/browser_smoke.py --asr` 在已安装 Python Playwright 的环境运行浏览器，ASR 后端使用本项目 `.venv`，全部测试数据隔离。

时间戳是模型估计，中文分词粒度由模型决定。当前提供录后转写与按文字手动选段，未实现流式 ASR、自动口吃/重复删除。

## 手动剪辑与渲染

`POST /decisions` 接受 `{"startMs":500,"endMs":1000}`，创建已接受的 `DELETE_SELECTION` 用户决策。时间为剪辑前录音的整数毫秒；非法范围返回 422，保护冲突返回 409。

`POST /render` 不需要请求体，完成后返回 201：`version`、`sourceVersionId`、`removedDurationMs`、`cutRanges`、`stale`。应用已接受的删除和重录替换决策，拒绝待定项、未支持的静音压缩、越界、全删和保护冲突。没有剪辑时导出完整音频。

`GET /render` 返回最后一次成片；决策或 `RENDER_SOURCE` 已变化时 `stale=true`。版本音频接口增加 `?download=true`，以 attachment 提供下载，默认仍可内联播放。

`render_outputs` 表记录源版本、决策快照和实际删除区间，启动时自动建表。渲染只更新 `EXPORT_SOURCE`，保持 `TIMEBASE` 和 `RENDER_SOURCE`，避免重复渲染导致再次剪短。发布结果前在 SQLite 写事务内复查源与决策；有变化则返回 409，清理未发布文件。

渲染在同步路由的工作线程中运行，解码到临时磁盘 PCM 后分块拼接。WAV 保留源采样率/声道，量化为 16-bit；可选边界淡化，尚无静音压缩，解码超时 120 秒。每次输出独立文件，旧版本继续可下载。

## WebSocket

控制消息：

```json
{"type":"control","action":"start","settings":{"sampleRate":48000,"channelCount":1,"mimeType":"audio/webm;codecs=opus","echoCancellation":true}}
```

`settings` 可选，由浏览器音轨设置和 MediaRecorder 提供。其他 action 为 `pause`、`resume`、`stop`。开始录制后发送音频二进制帧；停止前必须先发完最后一帧。暂停状态允许接收缓冲尾片，停止后拒绝音频。

服务器推送 `level`、`quality_event`、`stage`、`enhancement_progress`、`enhancement_done` 或 `error`。无效 JSON 和控制状态错误会返回明确错误。正常断开或异常断线都会停止质量检测并分析已收到的录音；不存在、已停止的会话，`capture_mode=batch` 的会话（含导入中），以及第二个录制端都会被拒绝连接。

实时电平和质量检测约每 2.5 秒执行一次，录制中时长为轮询估算，停止后以解码时长校正。录制阶段不执行流式 ASR，不发送随机 `live_signal`。

### 基于转写的剪辑建议

在复核页完成本机转写后，点击“生成剪辑建议”。目前仅检测相邻的相同词或词组，以及独立的“嗯、恩、唔、呃、额、um、uh、erm”——中文部分包含 faster-whisper 常见的同音异形字，不止一种写法。重复候选保留最后一次，删除边界使用完整词的估计时间戳；不推断词内部的口吃边界，也不做语义纠错。规则最多比较 12 个词的重复单元，相邻词间隔不超过 1 秒，总跨度不超过 8 秒。

每条建议默认待确认，可点击“试听上下文”，然后接受或拒绝；全部处理后才能生成成片。规则匹配不代表应当删除，重复可能是强调，语气词也可能是有效回应。界面显示“文字规则建议”，接口中的 confidence=1 仅表示字面规则匹配，不代表删除正确率。

`POST /api/sessions/{id}/decisions/suggest` 接收 `{"transcriptionJobId":"asr_..."}`，返回 `addedCount` 和完整 `decisions`。重复生成跳过所有已有决策覆盖的区间（包括已拒绝和受保护的区间）。转写进行中、来源过期或缺少真实词级时间戳时返回 409。重新转写成功会清除未确认的旧规则建议；已接受、已拒绝和手动决策保留，失败则保留全部原有结果。

复核页的“拒绝待定建议”仅拒绝尚未确认的文字规则建议，保留手动决策、已有接受/拒绝结果及保护项。对应接口为 `POST /api/sessions/{id}/decisions/reject-suggestions`，重复调用不会改变已处理项。文字选段试听在选段结束后自动暂停；“试听上下文”从候选前 1 秒播放到候选后 2 秒（限制在录音范围内）。播放器仍可手动继续播放。

### 转写导出

完成真实转写后，复核页可下载 UTF-8 TXT 或 SRT。TXT 保留完整转写，SRT 根据词级时间戳按句末标点、至少 800 毫秒停顿、约 42 字符或 5 秒分段，不拆分词；时间重叠的词合并在同一条字幕中。这些分段上限因此可能被单个长词或重叠词超过。

字幕对应**剪辑前录音**，包含已标记删除的文字，不适用于剪辑后成片的时间轴。接口：`GET /api/sessions/{id}/transcript/export?transcriptionJobId=asr_...&format=txt`（或 `srt`）。转写版本过期、无真实词级时间戳或空转写返回 409；导出不会更改转写、剪辑决策或音频文件。

复核页的剪辑前试听和渲染均使用 `RENDER_SOURCE`；前端时间基准条也展示该角色。`TIMEBASE` 保持冻结，`ASR_SOURCE` 提供完整转写音频。`REVIEW_SOURCE` 为保留角色，当前不驱动复核播放器。

### 会话阶段状态

会话列表接口包含 `captureStatus`、`localPersistenceStatus`、`uploadStatus`、`serverMergeStatus`、`liveAnalysisStatus` 和 `processingStatus`。列表每 2 秒刷新，分别展示采集、浏览器保存、服务端接收、合并校验和增强处理。停止后尚未增强的会话进入增强页，不再回到不可继续录制的录制页。

浏览器尚无持久化或断点续传，详情及列表中的 `localPersistenceStatus` 统一输出 `NOT_SUPPORTED`；旧记录的 `PERSISTED` 曾表示服务端保存，不应解释为浏览器备份。服务端成功写入第一片音频后 `uploadStatus=RECEIVING`，停止并通过解码校验后为 `COMPLETED`；这不保证异常断线时浏览器未发送的尾片也已收到。合并失败单独显示，不用接收状态代替合并结果。增强处理状态不代表转写或成片渲染的完成状态。

### 成片字幕

生成成片时会保存当时已完成转写的快照。成片卡片提供“导出成片 SRT”，接口为 `GET /api/sessions/{id}/render/{version_id}/subtitles`。字幕按该成片实际采样率及删除边界映射到剪辑后时间轴；完整删除的词不导出，后续词扣除此前删除的采样时长。

修改剪辑选择或重新转写不会改变已有成片的字幕；过期成片的按钮明确标为“导出上一次成片 SRT”。如果删除区间穿过词内部，则返回 409，需按完整词调整后重新生成。缺少转写快照或没有剩余文字也返回 409。旧数据库自动补充可空快照字段；旧成片需在转写完成后重新生成才能提供字幕。原有 TXT / SRT 转写导出仍对应剪辑前录音。

### 平滑剪辑

复核页可勾选“平滑剪辑”。`POST /render` 可传 `{"smoothCuts":true}`，默认 false，兼容原有直接拼接行为。每个删除边界的保留音频最多淡入/淡出 5 毫秒，各声道使用相同线性增益；淡化长度不超过保留片段的一半，少于 4 个采样的极短片段跳过淡化。无删除区间时不改变音频。

该处理不重叠、不额外删减采样，保持成片时长和字幕映射。版本引擎 `pcm_cut_fade` 标记开启淡化的成片，UI 单独显示实际成片设置；开关只影响下一次生成。淡化旨在减轻拼接突变，不保证消除所有可闻接缝，尚未实现交叉淡化或静音压缩。

### 单条剪辑选择冲突

决策返回整数 `revision`。复核页 PATCH 时携带 `expectedRevision`，在 SQLite 写事务中比较修订号后保存。过期请求返回 409，`detail.code=decision_conflict`，`detail.current` 为当前决策，不覆盖已有选择。所有单条更新和批量接受/拒绝都会递增相关修订号。旧表启动时补充 revision=0。旧客户端未传 expectedRevision 时仍保留原有更新行为，因此这不是强制所有客户端使用的版本协议。

冲突面板展示双方选择，并提供“采用最新选择”（刷新）、“应用我的选择”（按最新修订重试）、“改为待确认”（按最新修订提交 null）、“暂不处理”（不写入，保留服务器返回的显示）。重试遇到新修改会再次提示。冲突未关闭时禁用生成成片。原始 ADR 四选项定义未随仓库提供，这四项是当前接受/拒绝流程的实现约定。保护项、已删除候选和渲染并发冲突仍按原有错误处理，不属于此面板。

### 恢复与并发补充

启动时将中断的增强任务（QUEUED / PROCESSING / QUALITY_CHECKING）标为 FAILED，并更新对应会话处理状态；保留已发布版本和音频角色，允许重新增强或渲染。此操作仅适用于当前单 worker 运行方式，不是任务自动续跑。

同样在启动时处理两类不该跨进程重启存活的中间状态：`live_analysis_status=FINALIZING`（终态化的解码/质量分析被中断）按解码失败语义标为 FAILED，用户可重新录制/导入；`capture_status=IMPORTING`（导入的上传或发布阶段被中断，此时还没跑到 `on_capture_stop`）直接退回 IDLE 允许重新导入，并清理残留的 `raw.webm.importing` 临时文件。

`on_capture_stop` 本身也做了幂等发布：进入时先在写事务里把 `live_analysis_status` 从"未开始/已失败"原子切到 FINALIZING，已经是 FINALIZED 或 FINALIZING 都直接返回。WS 断线兜底、HTTP `/stop` 兜底和导入接口都可能对同一会话并发触发这个函数；`_capture_stop_tasks` 只在进程内对 WS 路径去重，覆盖不到直接调用的导入路径，真正的互斥保证在这层数据库行锁上，不依赖调用方是谁。

批量确认与批量拒绝接口可接收 `{"expectedRevisions":{"d_xxx":2}}`，表示页面上完整决策列表的修订快照。校验与写入在同一事务中执行；新增、删除或修改任何决策均返回 409，整批不保存。前端始终提交快照并提示刷新核对；旧客户端不提供请求体时仍兼容原有行为。

前端会话加载使用请求序号防止旧会话回包覆盖当前角色。转写完成后的刷新若遇到编辑操作或过期请求，会重新读取，避免把已保存选择的显示回退。

转写成功后会自动生成相邻文字重复的待复核提示，与转写文字一起发布；复核页显示疑似重复数量并高亮对应文字。不会自动接受或删除，已接受／拒绝的选择保持不变。当前按相邻相同文字匹配（最多 12 个词单元，整组不超过 8 秒，词间间隔不超过 1 秒），不支持跨会话或改写后的语义重复识别。语气词仍通过“查找重复与语气词”手动查找。


## 剪辑中重录替换

`POST /api/sessions/{id}/retakes` 接收 multipart 表单：`file`（浏览器 MediaRecorder 音频）、`startMs`、`endMs`（原始时间轴整数毫秒）、`sourceVersionId`（当时试听的 RENDER_SOURCE），以及 `snapshot`（JSON 字符串，如 `{"expectedRevisions":{"d_xxx":2}}`，需包含完整决策列表）。返回 201 和已接受的 `REPLACE_AUDIO` 决策，含 `replacementVersionId`、`replacementDurationMs`。单次上传限 60 MB、解码时长大于零且不超过 5 分钟；按文件内容解码为独立 PCM WAV，不覆盖原音频。

解码前和发布时校验来源及决策修订快照。发布在 SQLite 写事务中完成：范围内完全包含、尚未拒绝的旧剪辑变为拒绝并递增 revision，新重录决策和 `retake` 音频版本同时保存。部分重叠或保护冲突、来源或修订变化返回 409；非法区间、空音频或解码失败返回 422，清理未发布文件。其他区间选择保持不变。拒绝重录可恢复该范围原音频，旧剪辑选择不自动恢复。

生成成片按原时间轴排序删除／替换，重录片段转换到当前完整源的采样率和声道数，按自身实际长度插入，支持多处不重叠重录。接受的重录之间、重录与删除／保护区间不得重叠。渲染指纹包含重录版本及其时长，发布前复核；重复渲染不会再次替换已剪成片。可选平滑剪辑同样作用于替换拼接边界。

渲染响应新增 `hasReplacements`。`removedDurationMs` 表示净缩短时长，负数表示新成片变长。剪辑决策表启动时兼容补充可空 `replacement_version_id`、`replacement_duration_ms`，旧选择和修订号保持不变。

左侧转写和波形仍对应原始录音。含重录的成片生成后会独立转写实际成片文件，完成后提供新文字与成片时间轴 TXT／SRT；失败可重试，不影响原文和剪辑选择。不含重录的成片仍可通过原有快照导出字幕。


## 重录成片的自动转写与字幕

含已应用重录的 `POST /render` 成功后自动创建 `render_transcription_jobs` 任务，后台识别实际 master WAV，进度返回于 `transcriptionJob`，有可用文字时 `transcriptReady=true`。该表启动时自动创建，独立于原录音的 `transcription_jobs` 和 `transcript_tokens`，不会修改原始剪辑坐标、决策或音频角色。

- `GET /api/sessions/{id}/render/{version_id}/transcript`：返回该版成片最新任务、上次成功任务 ID 和词级文字（成片毫秒时间轴）。
- `POST /api/sessions/{id}/render/{version_id}/transcription`：以 `{"language":"auto"}`（或 `zh`／`en`）开始或重试。相同成片已有活动任务时返回原任务，避免重复执行。
- `GET /api/sessions/{id}/render/{version_id}/subtitles?format=srt`：优先导出该成片已成功识别的字幕，时间不再经过原剪辑区间偏移。`format=txt` 导出成片文字。

每次识别结果绑定具体成片版本；新成片不会覆盖旧版文字。失败保留此前成功结果，无语音的成功任务明确返回空文字，导出返回 409。缺少 ASR 依赖会保存可见的失败状态，安装后可重试。服务重启将未完成的成片任务标为失败；仍需手动重试，不自动续跑。成片转写期间禁止删除所属会话，删除会话时一并清理该表记录。原录音与成片 ASR 共用模型锁，按本机单进程串行识别。

验证：`pytest tests/test_render_transcription.py -q` 覆盖版本隔离、时轴、失败重试、无语音、依赖缺失及删除／重启边界；`python tests/browser_smoke.py --master-transcript` 用受控识别结果测试实际重录、渲染、文字预览与字幕下载流程，不代表模型识别准确率测试。

真实模型验证：`.venv/bin/python tests/asr_smoke.py --master` 使用 macOS 中英文合成语音和已缓存的本机模型，验证重录后实际成片的识别、时间戳和 SRT 导出，并确认原转写保持不变；数据隔离在临时目录。
