/**
 * 演示数据。接后端时把这一层换成 fetch，组件不需要改动——
 * 所有组件只依赖 src/types/api.ts 的类型，不依赖本文件的具体取值。
 */
import type {
  AudioQualityReport,
  AudioVersion,
  EditDecision,
  EnhancementJob,
  LiveSignal,
  QualityEvent,
  RecordingSession,
  RoleAssignment,
  TranscriptToken,
  Warning,
} from '../types/api'

export const session: RecordingSession = {
  id: 'rs_8f2c41',
  projectId: 'proj_qz09',
  captureMode: 'live',
  analysisLocality: 'local',
  captureStatus: 'RECORDING',
  localPersistenceStatus: 'PERSISTED',
  uploadStatus: 'STREAMING',
  serverMergeStatus: 'NOT_STARTED',
  liveAnalysisStatus: 'ANALYZING',
  processingStatus: 'NOT_STARTED',
  deviceLabel: 'usb-mic',
  actualSampleRate: 48000,
  channelCount: 1,
  mimeType: 'audio/webm;codecs=opus',
  lookbackWindowMs: 15000,
  committedDurationMs: 724_310,
  totalDurationMs: 727_940,
  captureProcessing: {
    applied: true,
    unknown: false,
    echoCancellationRequested: false,
    echoCancellationActual: 'false',
    noiseSuppressionRequested: false,
    // 请求关闭但浏览器忽略了——这正是 ADR-036 要求分开记录的场景
    noiseSuppressionActual: 'true',
    autoGainControlRequested: false,
    autoGainControlActual: 'false',
  },
  warnings: [
    {
      code: 'CAPTURE_PROCESSING_DETECTED',
      message: '浏览器忽略了关闭噪声抑制的请求，降噪强度已锁定为轻度',
      stage: 'capture',
    },
  ],
}

export const guardEvents: QualityEvent[] = [
  {
    id: 'qe_3',
    eventType: 'clipping',
    severity: 'warning',
    clientTimestampMs: 712_400,
    message: '连续削波超过 5 秒，建议暂停并调低麦克风增益',
    metrics: { clippingRatio: 0.0081, truePeakDbtp: -0.1 },
  },
  {
    id: 'qe_2',
    eventType: 'noise_spike',
    severity: 'notice',
    clientTimestampMs: 604_900,
    message: '环境噪声升高约 12 dB，可能有设备启动',
    metrics: { noiseFloorDbfs: -48.2 },
  },
  {
    id: 'qe_1',
    eventType: 'low_volume',
    severity: 'notice',
    clientTimestampMs: 118_200,
    message: '声音偏小，请靠近麦克风',
    metrics: { rmsDbfs: -47.6 },
  },
]

export const liveSignals: LiveSignal[] = [
  { id: 's1', signalLevel: 'weak', signalType: 'filler', startMs: 62_000, endMs: 62_400, score: 0.41, text: '嗯' },
  { id: 's2', signalLevel: 'medium', signalType: 'stutter', startMs: 148_000, endMs: 149_100, score: 0.63, text: '这个这个' },
  { id: 's3', signalLevel: 'strong', signalType: 'repetition', startMs: 246_000, endMs: 252_800, score: 0.81, text: '我们先看第二组数据' },
  { id: 's4', signalLevel: 'weak', signalType: 'filler', startMs: 331_000, endMs: 331_500, score: 0.38, text: '然后' },
  { id: 's5', signalLevel: 'medium', signalType: 'stutter', startMs: 402_000, endMs: 403_200, score: 0.59, text: '它它' },
  { id: 's6', signalLevel: 'strong', signalType: 'repetition', startMs: 511_000, endMs: 518_600, score: 0.86, text: '所以结论是三点' },
  { id: 's7', signalLevel: 'weak', signalType: 'filler', startMs: 604_000, endMs: 604_400, score: 0.35, text: '那么' },
  { id: 's8', signalLevel: 'strong', signalType: 'repetition', startMs: 668_000, endMs: 675_200, score: 0.79, text: '下一步的计划' },
]

export const audioVersions: AudioVersion[] = [
  {
    id: 'av_orig',
    versionType: 'original',
    label: '原始录音',
    durationMs: 727_940,
    sampleCount: 34_941_120,
    sampleRate: 48000,
    latencyMs: 0,
    engine: '—',
    engineVersion: '—',
    determinism: 'deterministic',
    processingLocality: 'local',
  },
  {
    id: 'av_stream',
    versionType: 'stream_normalized',
    label: '流式标准化',
    durationMs: 727_940,
    sampleCount: 34_941_120,
    sampleRate: 16000,
    latencyMs: 0,
    engine: 'ffmpeg-chain',
    engineVersion: '6.1.1',
    determinism: 'deterministic',
    processingLocality: 'local',
  },
  {
    id: 'av_dn',
    versionType: 'denoised',
    label: '增强（均衡）',
    durationMs: 727_940,
    sampleCount: 34_941_120,
    sampleRate: 48000,
    latencyMs: 32,
    engine: 'dfn',
    engineVersion: '3.0.2',
    determinism: 'version_stable',
    processingLocality: 'self_hosted',
  },
]

export const roles: RoleAssignment[] = [
  { role: 'TIMEBASE', audioVersionId: 'av_stream' },
  { role: 'ASR_SOURCE', audioVersionId: 'av_stream' },
  { role: 'REVIEW_SOURCE', audioVersionId: 'av_dn', upgradedFrom: 'av_stream' },
  { role: 'RENDER_SOURCE', audioVersionId: 'av_dn' },
  { role: 'EXPORT_SOURCE', audioVersionId: 'av_orig' },
  { role: 'REFERENCE_SOURCE', audioVersionId: 'av_orig' },
]

export const quality: AudioQualityReport = {
  qualityGrade: 'FAIR',
  gradeReason: 'estimated_snr_below_20db',
  integratedLufs: -21.4,
  truePeakDbtp: -0.1,
  noiseFloorDbfs: -44.8,
  estimatedSnrDb: 17.2,
  clippingRatio: 0.0081,
  humProbability: 0.12,
  reverbScore: 0.31,
  speechCoverage: 0.78,
  silenceCoverage: 0.22,
  noiseTypes: ['稳态宽带噪声', '空调声', '键盘声'],
  contentType: 'single_speaker_speech',
  contentTypeConfidence: 0.93,
  contentTypeOverriddenByUser: false,
}

export const enhancement: EnhancementJob = {
  resultVersionId: null,
  errorMessage: null,
  id: 'ej_44a1',
  mode: 'light',
  status: 'COMPLETED_WITH_FALLBACK',
  progress: 1,
  enhancementSuccess: true,
  noiseReductionDb: 9.4,
  requestedChain: ['highpass', 'hum_notch', 'denoise:balanced', 'loudnorm', 'limiter'],
  actualChain: ['highpass', 'hum_notch', 'denoise:light', 'loudnorm', 'limiter'],
  warnings: [
    {
      code: 'DENOISE_MODE_DOWNGRADED',
      message: '检测到设备端已做过噪声抑制，强度回落至轻度以避免叠加损伤',
      stage: 'denoise',
    },
  ],
  voiceProtection: [
    { key: 'speech_energy_retention', label: '语音区能量保留', value: 0.92, unit: '', target: '≥ 0.85', pass: true },
    { key: 'unvoiced_consonant_retention', label: '清辅音保留', value: 0.87, unit: '', target: '≥ 0.80', pass: true },
    { key: 'hf_attenuation_db', label: '高频衰减', value: 1.6, unit: 'dB', target: '≤ 3.0', pass: true },
    { key: 'transient_loss_ratio', label: '音节起始损失', value: 0.08, unit: '', target: '≤ 0.15', pass: true },
    { key: 'phase_continuity_score', label: '相位连续性', value: 0.94, unit: '', target: '≥ 0.90', pass: true },
    { key: 'asr_cer_delta', label: 'CER 变化', value: 0.001, unit: '', target: '≤ +0.005', pass: true },
    { key: 'duration_delta_samples', label: '样本数差', value: 0, unit: '', target: '= 0', pass: true, hard: true },
  ],
}

export const decisions: EditDecision[] = [
  {
    id: 'd1',
    startMs: 62_000,
    endMs: 62_400,
    action: 'DELETE_FILLER',
    reason: '句首语气词，前后无语义依赖',
    text: '嗯',
    confidence: 0.91,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: false,
    accepted: true,
  },
  {
    id: 'd2',
    startMs: 148_000,
    endMs: 149_100,
    action: 'DELETE_STUTTER',
    reason: '相邻重复 Token，保留后者',
    text: '这个这个',
    confidence: 0.88,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: false,
    accepted: true,
  },
  {
    id: 'd3',
    startMs: 246_000,
    endMs: 252_800,
    action: 'DELETE_REPETITION',
    reason: '整句重读，后句语法完整度更高',
    text: '我们先看第二组数据',
    confidence: 0.81,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: false,
    accepted: null,
  },
  {
    id: 'd4',
    startMs: 296_000,
    endMs: 301_400,
    action: 'KEEP_PROTECTED',
    reason: '含数字与单位，实体完整性保护',
    text: '同比增长三百二十七个基点',
    confidence: 0.97,
    source: 'algorithm',
    hardProtected: true,
    reviewRequired: false,
    accepted: true,
  },
  {
    id: 'd5',
    startMs: 388_000,
    endMs: 393_200,
    action: 'COMPRESS_SILENCE',
    reason: '中段静音 5.2 秒，超过自动压缩上限',
    text: '（静音 5.2 秒）',
    confidence: 0.54,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: true,
    accepted: null,
  },
  {
    id: 'd6',
    startMs: 511_000,
    endMs: 518_600,
    action: 'DELETE_REPETITION',
    reason: '整句重读，两句评分接近，需人工确认删除方向',
    text: '所以结论是三点',
    confidence: 0.62,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: true,
    accepted: null,
  },
  {
    id: 'd7',
    startMs: 668_000,
    endMs: 675_200,
    action: 'DELETE_REPETITION',
    reason: '跨 15 秒窗口的重读，由终态化全量去重发现',
    text: '下一步的计划',
    confidence: 0.79,
    source: 'algorithm',
    hardProtected: false,
    reviewRequired: false,
    accepted: null,
  },
]

export const transcript: TranscriptToken[] = [
  { id: 't1', text: '好，', startMs: 58_000, state: 'committed' },
  { id: 't2', text: '嗯', startMs: 62_000, state: 'committed', cut: true },
  { id: 't3', text: '我们今天讲三件事。', startMs: 63_000, state: 'committed' },
  { id: 't4', text: '第一件是采集端的质量守卫，', startMs: 68_000, state: 'committed' },
  { id: 't5', text: '第二件是时间基准怎么定，', startMs: 74_000, state: 'committed' },
  { id: 't6', text: '第三件是复核阶段用哪一版音频。', startMs: 80_000, state: 'committed' },
  { id: 't7', text: '先说第一件。', startMs: 86_000, state: 'committed' },
  { id: 't8', text: '录制过程中如果只在结束后做质量分析，', startMs: 89_000, state: 'committed' },
  { id: 't9', text: '用户可能录满一小时才被告知素材不可用，', startMs: 95_000, state: 'committed' },
  { id: 't10', text: '这个代价太大了。', startMs: 101_000, state: 'committed' },
  { id: 't11', text: '所以我们把检测前移到', startMs: 105_000, state: 'provisional' },
  { id: 't12', text: '还能补救的时刻', startMs: 108_000, state: 'provisional' },
]

export const taskWarnings: Warning[] = [
  {
    code: 'REVIEW_SOURCE_UPGRADED',
    message: '增强音频已就绪，试听已切换至增强版本。已完成的复核不受影响。',
    stage: 'enhance',
  },
  {
    code: 'CLOCK_DRIFT_DETECTED',
    message: '检测到 1420 ppm 采样时钟漂移，录制期标记时间戳已按比例重映射',
    stage: 'merge',
  },
  {
    code: 'LONG_SILENCE_AUTO_COMPRESSED',
    message: '首尾长静音已自动裁剪，中段 5.2 秒静音转人工确认',
    stage: 'timeline',
  },
]

/** 波形包络。真实实现来自解码后的 RMS 分桶，这里用确定性伪随机保证渲染稳定。 */
export function envelope(n: number, seed = 7): number[] {
  const out: number[] = []
  let x = seed
  for (let i = 0; i < n; i++) {
    x = (x * 1103515245 + 12345) % 2147483648
    const r = x / 2147483648
    const speech = Math.sin((i / n) * Math.PI * 9) * 0.22 + 0.55
    const pause = i % 37 < 3 ? 0.12 : 1
    out.push(Math.max(0.06, Math.min(1, (speech + (r - 0.5) * 0.5) * pause)))
  }
  return out
}
