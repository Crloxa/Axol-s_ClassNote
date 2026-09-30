export interface LessonMeta {
  id: string
  name: string
  created_at: string
  slide_count: number
  source_count: number
  segment_count: number
}

export interface Slide {
  idx: number
  title: string
  text: string
}

export interface Source {
  id: number
  name: string
  kind: 'pdf' | 'docx' | 'md' | 'code'
  content: any
  created_at: string
}

export interface Segment {
  id: number
  start: number
  end: number
  text: string
  status: string
  highlight: number
  manual_slide: number | null
  created_at: string
}

export interface Marker {
  id: number
  slide_idx: number
  t: number
}

export interface ResultMeta {
  id: number
  mode: string
  engine: string
  created_at: string
}

export interface LessonData {
  lesson: { id: string; name: string; created_at: string }
  slides: Slide[]
  sources: Source[]
  segments: Segment[]
  markers: Marker[]
  results: ResultMeta[]
  recording: boolean
  settings: { stt_model: string; stt_language: string; keep_audio: boolean }
}

export interface ProviderInfo {
  id: string
  label: string
  api: 'openai' | 'anthropic'
  base_url: string
  model: string
  input_price: number
  has_key: boolean
}

export interface PreviewResult {
  token: string
  expires_at: string
  scope: Scope
  provider: { id: string; label: string; base_url: string; model: string; has_key: boolean } | null
  slides: { idx: number; title: string }[]
  sources: { name: string; kind: string }[]
  stats: {
    ppt_chars: number
    transcript_chars: number
    source_chars: number
    total_chars: number
    tokens_est: number
    cost_est: number | null
    segment_count: number
    transcript_range: [number | null, number | null]
  }
  mode_label: string
}

export interface Scope {
  mode: 'key_points' | 'supplement' | 'outline'
  engine: 'local' | 'model'
  provider: string | null
  slide_idxs: number[] | null
  source_ids: number[]
  instruction: string
}

export const MODE_LABELS: Record<string, string> = {
  key_points: '重点讲义',
  supplement: 'PPT 外补充',
  outline: '详细提纲',
}

export function fmtClock(sec: number | null | undefined): string {
  if (sec == null) return '--:--'
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const mm = `${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
  return h > 0 ? `${h}:${mm}` : mm
}
