/**
 * 类型与 openapi.yaml（AI CleanCut API 2.1.0）保持一致。
 * 契约变更时先改 openapi.yaml，再同步本文件——不要反过来。
 */

export type ClientPlatform = 'web' | 'ios' | 'android' | 'desktop'
export type CaptureMode = 'batch' | 'live'
export type AnalysisLocality = 'local'

export type ContentType =
  | 'single_speaker_speech'
  | 'multi_speaker_speech'
  | 'speech_with_music'
  | 'speech_with_overlapping_noise'
  | 'music_dominant'
  | 'no_speech'
  | 'unknown'

export type AudioRole =
  | 'TIMEBASE'
  | 'ASR_SOURCE'
  | 'REVIEW_SOURCE'
  | 'RENDER_SOURCE'
  | 'EXPORT_SOURCE'
  | 'REFERENCE_SOURCE'

export type Determinism = 'deterministic' | 'version_stable' | 'non_reproducible'

export type VersionType =
  | 'original'
  | 'normalized'
  | 'stream_normalized'
  | 'denoised'
  | 'retake'
  | 'master'

export type QualityGrade = 'EXCELLENT' | 'GOOD' | 'FAIR' | 'POOR' | 'UNUSABLE'

export type DenoiseMode = 'off' | 'light' | 'balanced' | 'strong' | 'custom'

/** 任务级警告：不占用 HTTP 状态码（ADR-032） */
export interface Warning {
  code: string
  message: string
  stage?: string
}

/** 实时质量守卫事件（Baseline §5.2.5）。两级：提醒不阻断，警告置顶。 */
export interface QualityEvent {
  id: string
  eventType:
    | 'low_volume'
    | 'clipping'
    | 'no_input'
    | 'far_field'
    | 'noise_spike'
    | 'bluetooth_degraded'
    | 'format_change'
    | 'device_lost'
    | 'no_speech'
    | 'echo_risk'
  severity: 'notice' | 'warning'
  clientTimestampMs: number
  message: string
  metrics?: Record<string, number | string>
}

/** 设备端处理状态（ADR-036）。请求值与实际值必须分开记录。 */
export interface CaptureProcessingState {
  applied: boolean
  unknown: boolean
  echoCancellationRequested: boolean
  echoCancellationActual: 'true' | 'false' | 'unknown'
  noiseSuppressionRequested: boolean
  noiseSuppressionActual: 'true' | 'false' | 'unknown'
  autoGainControlRequested: boolean
  autoGainControlActual: 'true' | 'false' | 'unknown'
}

/** 时钟漂移与编码器补偿（ADR-035） */
export interface ClockDriftReport {
  decodedSampleCount: number
  expectedSampleCount: number
  clockDriftPpm: number
  encoderDelaySamples: number
  paddingSamples: number
  driftRemapApplied: boolean
}

export interface RecordingSession {
  id: string
  projectId: string
  captureMode: CaptureMode
  analysisLocality: AnalysisLocality
  captureStatus: 'IDLE' | 'RECORDING' | 'PAUSED' | 'STOPPED' | 'IMPORTING' | 'DEVICE_LOST' | 'RECONNECTING' | 'ABANDONED'
  localPersistenceStatus: 'PENDING' | 'PERSISTED' | 'PARTIAL' | 'LOST'
  uploadStatus: 'NOT_STARTED' | 'UPLOADING' | 'STREAMING' | 'PAUSED_BY_POLICY' | 'COMPLETED' | 'FAILED'
  serverMergeStatus: 'NOT_STARTED' | 'MERGING' | 'VERIFIED' | 'FAILED'
  liveAnalysisStatus:
    | 'NOT_APPLICABLE'
    | 'ANALYZING'
    | 'FINALIZING'
    | 'FINALIZED'
    | 'FINALIZE_DEGRADED'
  processingStatus: 'NOT_STARTED' | 'QUEUED' | 'PROCESSING' | 'DONE' | 'FAILED'
  deviceLabel: string
  actualSampleRate: number
  channelCount: number
  mimeType: string
  lookbackWindowMs: number
  committedDurationMs: number
  totalDurationMs: number
  captureProcessing: CaptureProcessingState
  clockDrift?: ClockDriftReport
  warnings: Warning[]
}

/** 实时信号——提示，不是判定（ADR-053） */
export interface LiveSignal {
  id: string
  signalLevel: 'weak' | 'medium' | 'strong'
  signalType: 'filler' | 'stutter' | 'repetition'
  startMs: number
  endMs: number
  score: number
  text: string
  becameFinalDecision?: boolean
  invalidated?: boolean
  invalidationReason?: string
}

export interface AudioVersion {
  id: string
  versionType: VersionType
  label: string
  durationMs: number
  sampleCount: number
  sampleRate: number
  latencyMs: number
  engine: string
  engineVersion: string
  determinism: Determinism
  processingLocality: 'local' | 'self_hosted' | 'third_party'
}

export interface RoleAssignment {
  role: AudioRole
  audioVersionId: string
  upgradedFrom?: string
}

export interface AudioQualityReport {
  qualityGrade: QualityGrade
  gradeReason: string
  integratedLufs: number
  truePeakDbtp: number
  noiseFloorDbfs: number
  estimatedSnrDb: number
  clippingRatio: number
  humProbability: number
  reverbScore: number
  speechCoverage: number
  silenceCoverage: number
  noiseTypes: string[]
  contentType: ContentType
  contentTypeConfidence: number
  contentTypeOverriddenByUser: boolean
}

/** 语音保护指标（ADR-044）。复合判据，不以噪声下降单独判成功。 */
export interface VoiceProtectionMetric {
  key: string
  label: string
  value: number
  unit: string
  target: string
  pass: boolean
  hard?: boolean
}

export interface EnhancementJob {
  resultVersionId: string | null
  errorMessage: string | null
  id: string
  mode: DenoiseMode
  status: 'QUEUED' | 'PROCESSING' | 'QUALITY_CHECKING' | 'FALLING_BACK' | 'COMPLETED' | 'COMPLETED_WITH_FALLBACK' | 'FAILED'
  progress: number
  enhancementSuccess: boolean | null
  noiseReductionDb: number
  voiceProtection: VoiceProtectionMetric[]
  requestedChain: string[]
  actualChain: string[]
  warnings: Warning[]
}

export type DecisionAction =
  | 'REPLACE_AUDIO'
  | 'DELETE_SELECTION'
  | 'DELETE_REPETITION'
  | 'DELETE_STUTTER'
  | 'DELETE_FILLER'
  | 'COMPRESS_SILENCE'
  | 'KEEP_PROTECTED'

export interface EditDecision {
  replacementVersionId?: string | null
  replacementDurationMs?: number | null
  revision?: number
  id: string
  startMs: number
  endMs: number
  action: DecisionAction
  reason: string
  text: string
  confidence: number
  source: 'algorithm' | 'user' | 'transcript_rule' | 'silence_rule'
  hardProtected: boolean
  reviewRequired: boolean
  accepted: boolean | null
}

export interface TranscriptToken {
  endMs?: number | null
  jobId?: string | null
  id: string
  text: string
  startMs: number
  state: 'committed' | 'provisional'
  cut?: boolean
}

export interface RenderOutput {
  transcriptionJob?: TranscriptionJob | null
  transcriptReady?: boolean
  hasReplacements?: boolean
  version: AudioVersion
  sourceVersionId: string
  removedDurationMs: number
  cutRanges: { startMs: number; endMs: number }[]
  stale: boolean
  loudness?: LoudnessResult | null
}

/** 章节：startMs 在原始（剪辑前）时间轴上。 */
export interface Chapter {
  id: string
  startMs: number
  title: string
}

/** 换算到某个音频版本时间轴后的章节。moved 表示原起点落在被剪掉/重录的区间里，已挪到该处。 */
export interface MappedChapter extends Chapter {
  endMs: number
  originalStartMs: number
  moved: boolean
}

export type LoudnessTarget = 'podcast' | 'broadcast'

/** 成片响度标准化结果；inputLufs 为 null 表示近乎静音，未做调整。 */
export interface LoudnessResult {
  target: LoudnessTarget
  targetLufs: number
  inputLufs: number | null
  inputTruePeakDb: number | null
  gainDb: number
  outputLufs: number | null
  /** null=已达到目标；peak=受真峰值上限约束；max_gain=提升幅度已到上限 */
  limitedBy: 'peak' | 'max_gain' | null
}

export interface TranscriptionJob {
  id: string
  sourceVersionId: string
  model: string
  requestedLanguage: string
  detectedLanguage: string | null
  status: 'QUEUED' | 'LOADING_MODEL' | 'TRANSCRIBING' | 'COMPLETED' | 'FAILED'
  progress: number
  errorMessage: string | null
}

export interface RenderTranscript {
  versionId: string
  job: TranscriptionJob | null
  transcriptJobId: string | null
  tokens: { text: string; startMs: number; endMs: number }[]
}
