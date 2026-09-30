import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as api from './api'
import {
  fmtClock, MODE_LABELS,
  type LessonData, type LessonMeta, type PreviewResult, type ProviderInfo, type Segment,
} from './types'

type RecState = 'idle' | 'recording' | 'paused'
type ScopeMode = 'all' | 'chapters'
type Mode = 'key_points' | 'supplement' | 'outline'
type Engine = 'local' | 'model'

const CODE_ACCEPT =
  '.pdf,.docx,.md,.markdown,.py,.js,.ts,.tsx,.jsx,.java,.go,.rs,.c,.cpp,.h,.cs,.sql,.json,.yaml,.yml,.toml,.sh'

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export default function App() {
  // ---- 基础数据 ----
  const [lessons, setLessons] = useState<LessonMeta[]>([])
  const [lesson, setLesson] = useState<LessonData | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')

  // ---- 录音 ----
  const [recState, setRecState] = useState<RecState>('idle')
  const [recSeconds, setRecSeconds] = useState(0)
  const recRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const recStartWall = useRef(0)
  const pausedAccum = useRef(0)
  const pauseAt = useRef(0)

  // ---- 范围与生成 ----
  const [scopeMode, setScopeMode] = useState<ScopeMode>('all')
  const [checkedSlides, setCheckedSlides] = useState<Set<number>>(new Set())
  const [checkedSources, setCheckedSources] = useState<Set<number>>(new Set())
  const [mode, setMode] = useState<Mode>('key_points')
  const [engine, setEngine] = useState<Engine>('local')
  const [providerId, setProviderId] = useState('deepseek')
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [pcfg, setPcfg] = useState({ base_url: '', model: '', input_price: 0 })
  const [keyInput, setKeyInput] = useState('')
  const [instruction, setInstruction] = useState('')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [result, setResult] = useState<{ id: number; content: string; dirty: boolean } | null>(null)

  // ---- 浏览选中项 ----
  const [currentSlide, setCurrentSlide] = useState(0)
  const [viewSourceId, setViewSourceId] = useState<number | null>(null)
  const [editSeg, setEditSeg] = useState<{ id: number; text: string } | null>(null)

  const withBusy = async (label: string, fn: () => Promise<void>) => {
    setBusy(label)
    try { await fn() } catch (e) { setErr(msg(e)) } finally { setBusy('') }
  }

  const loadLessons = useCallback(async () => {
    const ls = await api.get<LessonMeta[]>('/lessons')
    setLessons(ls)
    return ls
  }, [])

  const loadLesson = useCallback(async (id: string) => {
    const data = await api.get<LessonData>(`/lessons/${id}`)
    setLesson(data)
    setCheckedSlides(new Set(data.slides.map(s => s.idx)))
    setCheckedSources(new Set(data.sources.map(s => s.id)))
    setCurrentSlide(data.slides.length ? 1 : 0)
    setViewSourceId(null)
    if (data.recording) setRecState('recording') // 后端有活动会话（如页面刷新后）
    return data
  }, [])

  // 首次加载
  useEffect(() => {
    (async () => {
      try {
        const ls = await loadLessons()
        if (ls.length) await loadLesson(ls[0].id)
        const ps = await api.get<ProviderInfo[]>('/providers')
        setProviders(ps)
        const p = ps.find(x => x.id === 'deepseek') ?? ps[0]
        if (p) { setProviderId(p.id); setPcfg({ base_url: p.base_url, model: p.model, input_price: p.input_price }) }
      } catch (e) { setErr(msg(e)) }
    })()
  }, [loadLessons, loadLesson])

  // 录音计时（音频时间轴 = 排除暂停后的真实录音时长）
  useEffect(() => {
    if (recState === 'idle') return
    const t = setInterval(() => {
      const now = recState === 'paused' ? pauseAt.current : Date.now()
      setRecSeconds((now - recStartWall.current + pausedAccum.current) / 1000)
    }, 500)
    return () => clearInterval(t)
  }, [recState])

  // 录音期间定期增量转写
  useEffect(() => {
    if (recState === 'idle' || !lesson) return
    const t = setInterval(async () => {
      try {
        const r = await api.post<{ segments: Segment[] }>(`/lessons/${lesson.lesson.id}/transcribe/flush`)
        if (r.segments.length) {
          const data = await api.get<LessonData>(`/lessons/${lesson.lesson.id}`)
          setLesson(data)
        }
      } catch { /* 静默重试 */ }
    }, 10000)
    return () => clearInterval(t)
  }, [recState, lesson])

  // 快捷键 M：标记当前 PPT 页
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.key.toLowerCase() === 'm' && recState !== 'idle') void markCurrentPage()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  const refreshLesson = async () => {
    if (!lesson) return
    setLesson(await api.get<LessonData>(`/lessons/${lesson.lesson.id}`))
  }

  // ---- 录音控制 ----
  const startRecording = () => withBusy('正在开始录音…', async () => {
    if (!lesson) return
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    streamRef.current = stream
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : ''
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 96000 } : undefined)
    recRef.current = rec
    const lid = lesson.lesson.id
    rec.ondataavailable = e => {
      if (e.data && e.data.size > 0)
        void fetch(`/api/lessons/${lid}/audio`, { method: 'POST', body: e.data })
    }
    await api.post(`/lessons/${lid}/recording/start`)
    rec.start(2000)
    recStartWall.current = Date.now()
    pausedAccum.current = 0
    pauseAt.current = 0
    setRecSeconds(0)
    setRecState('recording')
  })

  const pauseRecording = () => {
    recRef.current?.pause()
    pauseAt.current = Date.now()
    setRecState('paused')
  }

  const resumeRecording = () => {
    pausedAccum.current += pauseAt.current - recStartWall.current
    recStartWall.current = Date.now()
    pauseAt.current = 0
    recRef.current?.resume()
    setRecState('recording')
  }

  const stopRecording = () => withBusy('正在停止并完成转写…', async () => {
    if (!lesson) return
    const rec = recRef.current
    if (rec && rec.state !== 'inactive') {
      await new Promise<void>(done => { rec.onstop = () => done(); rec.stop() })
    }
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    recRef.current = null
    await api.post(`/lessons/${lesson.lesson.id}/recording/stop`)
    await refreshLesson()
    setRecState('idle')
  })

  const markCurrentPage = async () => {
    if (!lesson || recState === 'idle') return
    if (!currentSlide) { setErr('请先在左侧章节列表中点击要标记的 PPT 页'); return }
    try {
      await api.post(`/lessons/${lesson.lesson.id}/markers`,
        { slide_idx: currentSlide, t: Number(recSeconds.toFixed(2)) })
      await refreshLesson()
    } catch (e) { setErr(msg(e)) }
  }

  // ---- 文件导入 ----
  const importPpt = (f: File) => withBusy('正在解析 PPT…', async () => {
    if (!lesson) return
    await api.upload(`/lessons/${lesson.lesson.id}/ppt`, f)
    await refreshLesson()
    setCurrentSlide(1)
    setViewSourceId(null)
  })

  const importSource = (f: File) => withBusy(`正在解析 ${f.name}…`, async () => {
    if (!lesson) return
    await api.upload(`/lessons/${lesson.lesson.id}/sources`, f)
    await refreshLesson()
  })

  // ---- 转写编辑 ----
  const saveSegment = async () => {
    if (!editSeg) return
    await withBusy('保存中…', async () => {
      await api.patch(`/segments/${editSeg.id}`, { text: editSeg.text })
      setEditSeg(null)
      await refreshLesson()
    })
  }
  const toggleHighlight = async (s: Segment) => {
    await api.patch(`/segments/${s.id}`, { highlight: !s.highlight })
    await refreshLesson()
  }
  const setSegSlide = async (s: Segment, slide: number | null) => {
    await api.patch(`/segments/${s.id}`, { manual_slide: slide })
    await refreshLesson()
  }

  // ---- 生成 ----
  const buildScope = () => ({
    mode, engine,
    provider: engine === 'model' ? providerId : null,
    slide_idxs: scopeMode === 'all' ? null : [...checkedSlides],
    source_ids: [...checkedSources],
    instruction,
  })

  const doPreview = () => withBusy('正在计算预览…', async () => {
    if (!lesson) return
    const p = await api.post<PreviewResult>('/generate/preview',
      { lesson_id: lesson.lesson.id, ...buildScope() })
    setPreview(p)
  })

  const confirmSend = () => withBusy('正在生成（模型调用中）…', async () => {
    if (!preview || !lesson) return
    const r = await api.post<{ result_id: number; content_md: string }>('/generate',
      { token: preview.token })
    setPreview(null)
    setResult({ id: r.result_id, content: r.content_md, dirty: false })
    await refreshLesson()
  })

  const saveProviderCfg = () => withBusy('保存服务商配置…', async () => {
    await api.put(`/providers/${providerId}`, pcfg)
    const ps = await api.get<ProviderInfo[]>('/providers')
    setProviders(ps)
  })

  const saveKey = () => withBusy('保存 API Key（Windows 凭据管理器）…', async () => {
    await api.put(`/providers/${providerId}/key`, { key: keyInput })
    setKeyInput('')
    const ps = await api.get<ProviderInfo[]>('/providers')
    setProviders(ps)
  })

  const deleteKey = () => withBusy('删除 API Key…', async () => {
    await api.del(`/providers/${providerId}/key`)
    const ps = await api.get<ProviderInfo[]>('/providers')
    setProviders(ps)
  })

  const saveSettings = async (patch: Partial<LessonData['settings']>) => {
    if (!lesson) return
    await api.put('/settings', patch)
    await refreshLesson()
  }

  const openResult = async (rid: number) => {
    const r = await api.get<{ id: number; content_md: string }>(`/results/${rid}`)
    setResult({ id: r.id, content: r.content_md, dirty: false })
  }

  const saveResult = () => withBusy('保存结果…', async () => {
    if (!result) return
    await api.patch(`/results/${result.id}`, { content_md: result.content })
    setResult({ ...result, dirty: false })
  })

  const newLesson = async (name: string) => withBusy('创建课堂…', async () => {
    await api.post('/lessons', { name })
    const ls = await loadLessons()
    if (ls.length) await loadLesson(ls[0].id)
  })

  const provider = providers.find(p => p.id === providerId)
  const viewSource = useMemo(
    () => lesson?.sources.find(s => s.id === viewSourceId) ?? null, [lesson, viewSourceId])
  const sortedSegments = useMemo(
    () => lesson ? [...lesson.segments].sort((a, b) => a.start - b.start) : [], [lesson])

  // ============ 渲染 ============
  return (
    <div className="app">
      <header>
        <div className="brand">📚 课堂复习 Agent</div>
        <div className="lesson-switch">
          <select
            value={lesson?.lesson.id ?? ''}
            onChange={e => void withBusy('切换课堂…', async () => {
              await loadLesson(e.target.value); setResult(null)
            })}
          >
            {lessons.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}
            {!lessons.length && <option value="">（尚无课堂）</option>}
          </select>
          <button
            onClick={() => {
              const name = window.prompt('新课堂名称：')
              if (name && name.trim()) void newLesson(name.trim())
            }}
          >＋ 新建课堂</button>
        </div>
        <div className="rec-zone">
          {recState === 'idle' ? (
            <button className="primary" disabled={!lesson || !!busy}
              onClick={() => void startRecording()}>● 开始记录</button>
          ) : (
            <>
              <span className={`rec-pill ${recState}`}>
                {recState === 'recording' ? '● 录音中' : '⏸ 已暂停'} {fmtClock(recSeconds)}
              </span>
              <button disabled={!currentSlide} title="快捷键 M：把之后的转写归属到当前页"
                onClick={() => void markCurrentPage()}>标记当前页（第 {currentSlide || '-'} 页）</button>
              {recState === 'recording'
                ? <button onClick={pauseRecording}>暂停</button>
                : <button onClick={resumeRecording}>继续</button>}
              <button className="danger" onClick={() => void stopRecording()}>停止</button>
            </>
          )}
        </div>
      </header>

      {err && <div className="error-bar">⚠ {err} <button onClick={() => setErr('')}>×</button></div>}
      {busy && <div className="busy-bar">{busy}</div>}

      {!lesson ? (
        <div className="empty">请先创建一个课堂。</div>
      ) : (
        <main>
          {/* ============ 左栏 ============ */}
          <aside className="col-left">
            <section>
              <h3>主 PPT</h3>
              {lesson.slides.length ? (
                <>
                  <div className="scope-radios">
                    <label><input type="radio" checked={scopeMode === 'all'}
                      onChange={() => setScopeMode('all')} /> 全部内容</label>
                    <label><input type="radio" checked={scopeMode === 'chapters'}
                      onChange={() => setScopeMode('chapters')} /> 选定章节</label>
                  </div>
                  <ul className="slide-list">
                    {lesson.slides.map(s => (
                      <li key={s.idx} className={s.idx === currentSlide ? 'active' : ''}>
                        {scopeMode === 'chapters' && (
                          <input type="checkbox" checked={checkedSlides.has(s.idx)}
                            onChange={e => {
                              const next = new Set(checkedSlides)
                              e.target.checked ? next.add(s.idx) : next.delete(s.idx)
                              setCheckedSlides(next)
                            }} />
                        )}
                        <button className="slide-btn"
                          onClick={() => { setCurrentSlide(s.idx); setViewSourceId(null) }}>
                          <span className="page-no">第 {s.idx} 页</span> {s.title}
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              ) : <p className="hint">尚未导入主 PPTX。</p>}
              <label className="file-btn">
                导入 / 替换主 PPTX
                <input type="file" accept=".pptx" hidden
                  onChange={e => { const f = e.target.files?.[0]; if (f) void importPpt(f); e.target.value = '' }} />
              </label>
            </section>

            <section>
              <h3>参考资料（{lesson.sources.length}）</h3>
              <ul className="source-list">
                {lesson.sources.map(s => (
                  <li key={s.id}>
                    <input type="checkbox" checked={checkedSources.has(s.id)}
                      title="生成时包含这份资料"
                      onChange={e => {
                        const next = new Set(checkedSources)
                        e.target.checked ? next.add(s.id) : next.delete(s.id)
                        setCheckedSources(next)
                      }} />
                    <button className="slide-btn" onClick={() => setViewSourceId(s.id)}>
                      <span className={`kind kind-${s.kind}`}>{s.kind}</span> {s.name}
                    </button>
                    <button className="x" title="删除"
                      onClick={() => void withBusy('删除中…', async () => {
                        await api.del(`/sources/${s.id}`); await refreshLesson()
                      })}>×</button>
                  </li>
                ))}
              </ul>
              <label className="file-btn">
                ＋ 添加 PDF / DOCX / MD / 代码
                <input type="file" accept={CODE_ACCEPT} hidden
                  onChange={e => { const f = e.target.files?.[0]; if (f) void importSource(f); e.target.value = '' }} />
              </label>
            </section>

            <section>
              <h3>转写设置</h3>
              <label className="row">模型
                <select value={lesson.settings.stt_model}
                  onChange={e => void saveSettings({ stt_model: e.target.value })}>
                  <option value="tiny">tiny（最快）</option>
                  <option value="base">base</option>
                  <option value="small">small</option>
                  <option value="medium">medium（最准）</option>
                </select>
              </label>
              <label className="row">语言
                <input value={lesson.settings.stt_language}
                  onChange={e => void saveSettings({ stt_language: e.target.value })} />
              </label>
              <label className="row">
                <input type="checkbox" checked={lesson.settings.keep_audio}
                  onChange={e => void saveSettings({ keep_audio: e.target.checked })} />
                保留原始音频（取消则只留转写）
              </label>
            </section>
          </aside>

          {/* ============ 中栏 ============ */}
          <section className="col-mid">
            <div className="mid-preview">
              {viewSource ? (
                <>
                  <h3><span className={`kind kind-${viewSource.kind}`}>{viewSource.kind}</span> {viewSource.name}</h3>
                  <SourceView src={viewSource} />
                </>
              ) : currentSlide && lesson.slides.find(s => s.idx === currentSlide) ? (
                (() => {
                  const s = lesson.slides.find(x => x.idx === currentSlide)!
                  return (
                    <>
                      <h3>第 {s.idx} 页 / 共 {lesson.slides.length} 页 · {s.title}</h3>
                      <pre className="slide-text">{s.text || '（本页未提取到文字）'}</pre>
                    </>
                  )
                })()
              ) : (
                <p className="hint">在左侧点击章节或资料进行预览。</p>
              )}
            </div>

            <div className="mid-transcript">
              <h3>课堂转写（{sortedSegments.length} 条{lesson.markers.length ? ` · ${lesson.markers.length} 个页面标记` : ''}）</h3>
              {!sortedSegments.length && (
                <p className="hint">点击右上角“开始记录”后，这里会出现带时间戳的转写流。</p>
              )}
              <ul>
                {sortedSegments.map(seg => {
                  const editing = editSeg?.id === seg.id
                  return (
                    <li key={seg.id} className={seg.highlight ? 'hl' : ''}>
                      <span className="t">{fmtClock(seg.start)}–{fmtClock(seg.end)}</span>
                      {seg.manual_slide != null
                        ? <span className="tag">PPT 第 {seg.manual_slide} 页</span>
                        : <span className="tag none">未关联</span>}
                      {seg.status === 'error' && <span className="tag err">待核对</span>}
                      {editing ? (
                        <span className="seg-edit">
                          <textarea value={editSeg.text} rows={2}
                            onChange={e => setEditSeg({ id: seg.id, text: e.target.value })} />
                          <button className="primary" onClick={() => void saveSegment()}>保存</button>
                          <button onClick={() => setEditSeg(null)}>取消</button>
                        </span>
                      ) : (
                        <span className="seg-text">{seg.text}</span>
                      )}
                      <span className="seg-ops">
                        <button title="重点标记"
                          onClick={() => void toggleHighlight(seg)}>{seg.highlight ? '★' : '☆'}</button>
                        <button title="修正文字" onClick={() => setEditSeg({ id: seg.id, text: seg.text })}>改</button>
                        <select title="手动关联到 PPT 页"
                          value={seg.manual_slide ?? ''}
                          onChange={e => void setSegSlide(seg, e.target.value === '' ? null : Number(e.target.value))}>
                          <option value="">关联…</option>
                          {lesson.slides.map(s => <option key={s.idx} value={s.idx}>第 {s.idx} 页</option>)}
                        </select>
                      </span>
                    </li>
                  )
                })}
              </ul>
            </div>
          </section>

          {/* ============ 右栏 ============ */}
          <aside className="col-right">
            <section>
              <h3>生成</h3>
              <label className="row">模式
                <select value={mode} onChange={e => setMode(e.target.value as Mode)}>
                  {Object.entries(MODE_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </label>
              <div className="scope-radios">
                <label><input type="radio" checked={engine === 'local'}
                  onChange={() => setEngine('local')} /> 本地规则整理</label>
                <label><input type="radio" checked={engine === 'model'}
                  onChange={() => setEngine('model')} /> 远程模型</label>
              </div>
              <label className="row">补充指令（只影响输出形式）
                <textarea rows={2} value={instruction}
                  onChange={e => setInstruction(e.target.value)}
                  placeholder="如：用表格整理易错点" />
              </label>
              <button className="primary wide" disabled={!!busy} onClick={() => void doPreview()}>
                生成预览 →
              </button>
            </section>

            {engine === 'model' && (
              <section>
                <h3>模型服务商</h3>
                <label className="row">服务商
                  <select value={providerId} onChange={e => {
                    setProviderId(e.target.value)
                    const p = providers.find(x => x.id === e.target.value)
                    if (p) setPcfg({ base_url: p.base_url, model: p.model, input_price: p.input_price })
                  }}>
                    {providers.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
                  </select>
                </label>
                <label className="row">端点
                  <input value={pcfg.base_url}
                    onChange={e => setPcfg({ ...pcfg, base_url: e.target.value })} />
                </label>
                <label className="row">模型名
                  <input value={pcfg.model}
                    onChange={e => setPcfg({ ...pcfg, model: e.target.value })} />
                </label>
                <label className="row">输入价格（元/百万token）
                  <input type="number" min={0} step="0.1" value={pcfg.input_price}
                    onChange={e => setPcfg({ ...pcfg, input_price: Number(e.target.value) })} />
                </label>
                <button className="wide" onClick={() => void saveProviderCfg()}>保存端点/模型配置</button>
                <div className="key-row">
                  <input type="password" placeholder={provider?.has_key ? '已保存 Key，可更换' : '输入 API Key'}
                    value={keyInput} onChange={e => setKeyInput(e.target.value)} />
                  <button disabled={!keyInput} onClick={() => void saveKey()}>保存Key</button>
                  {provider?.has_key && <button onClick={() => void deleteKey()}>删除</button>}
                </div>
                <p className="hint">
                  Key 存于 Windows 凭据管理器；状态：
                  {provider?.has_key ? '✅ 已保存' : '❌ 未保存'}
                </p>
              </section>
            )}

            <section>
              <h3>生成结果</h3>
              <ul className="result-list">
                {lesson.results.map(r => (
                  <li key={r.id}>
                    <button className="link" onClick={() => void openResult(r.id)}>
                      #{r.id} {MODE_LABELS[r.mode] ?? r.mode}
                      {r.engine === 'local' ? '（本地）' : ''} · {r.created_at.slice(5, 16)}
                    </button>
                  </li>
                ))}
                {!lesson.results.length && <li className="hint">还没有生成结果。</li>}
              </ul>
              {result && (
                <div className="result-editor">
                  <textarea value={result.content} rows={16}
                    onChange={e => setResult({ ...result, content: e.target.value, dirty: true })} />
                  <div className="btn-row">
                    <button disabled={!result.dirty} onClick={() => void saveResult()}>保存修改</button>
                    <a className="btn" href={`/api/results/${result.id}/export?format=md`}>导出 MD</a>
                    <a className="btn" href={`/api/results/${result.id}/export?format=html`}>导出 HTML</a>
                  </div>
                </div>
              )}
            </section>
          </aside>
        </main>
      )}

      {/* ============ 发送预览弹窗 ============ */}
      {preview && (
        <div className="modal-mask">
          <div className="modal">
            <h3>确认发送（{preview.mode_label}）</h3>
            {preview.provider ? (
              <p><b>服务商：</b>{preview.provider.label} · {preview.provider.base_url} · 模型 {preview.provider.model}
                {preview.provider.has_key ? '' : '（⚠ 未保存 API Key）'}</p>
            ) : <p><b>引擎：</b>本地规则整理（不联网）</p>}
            <p><b>章节：</b>{preview.scope.slide_idxs == null
              ? '全部内容（全部 ' + preview.slides.length + ' 页）'
              : preview.slides.map(s => `第 ${s.idx} 页`).join('、') || '（无）'}</p>
            <p><b>参考资料：</b>{preview.sources.length
              ? preview.sources.map(s => s.name).join('、') : '（未勾选）'}</p>
            <p><b>转写：</b>{preview.stats.segment_count} 段，
              时间 {fmtClock(preview.stats.transcript_range[0])} – {fmtClock(preview.stats.transcript_range[1])}</p>
            <table className="stats">
              <tbody>
                <tr><td>PPT 文本</td><td>{preview.stats.ppt_chars} 字符</td></tr>
                <tr><td>参考资料文本</td><td>{preview.stats.source_chars} 字符</td></tr>
                <tr><td>转写文本</td><td>{preview.stats.transcript_chars} 字符</td></tr>
                <tr><td>合计</td><td>{preview.stats.total_chars} 字符 ≈ {preview.stats.tokens_est} tokens</td></tr>
                {preview.stats.cost_est != null &&
                  <tr><td>估算费用</td><td>≈ ¥{preview.stats.cost_est}</td></tr>}
              </tbody>
            </table>
            <p className="hint">原始文件与音频不会发送；只发送以上选中范围的提取文本。令牌 {preview.expires_at} 过期。</p>
            <div className="btn-row">
              <button onClick={() => setPreview(null)}>取消</button>
              <button onClick={() => setPreview(null)}>返回修改范围</button>
              <button className="primary"
                disabled={preview.scope.engine === 'model' && !preview.provider?.has_key}
                onClick={() => void confirmSend()}>确认发送</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------- 参考资料预览 ----------
function SourceView({ src }: { src: import('./types').Source }) {
  const c = src.content ?? {}
  if (src.kind === 'pdf') {
    return <>{(c.pages ?? []).map((p: any) => (
      <div key={p.page} className="src-block">
        <b>第 {p.page} 页</b>
        {p.empty ? <p className="hint">未提取文字（可能是扫描页）</p> : <pre>{p.text}</pre>}
      </div>
    ))}</>
  }
  if (src.kind === 'docx') {
    return <>{(c.blocks ?? []).map((b: any, i: number) => (
      <div key={i} className="src-block">
        {b.path ? <div className="src-path">{b.path}</div> : null}
        <pre className={b.type === 'heading' ? 'head' : ''}>{b.text}</pre>
      </div>
    ))}</>
  }
  if (src.kind === 'md') {
    return <>{(c.sections ?? []).map((s: any, i: number) => (
      <div key={i} className="src-block">
        <div className="src-path">{s.path}</div>
        <pre className={s.level ? 'head' : ''}>{s.text}</pre>
      </div>
    ))}</>
  }
  if (src.kind === 'code') {
    const lines: string[] = (c.text ?? '').split('\n')
    return (
      <pre>{lines.map((ln, i) => (
        <div key={i}><span className="lineno">{i + 1}</span> {ln}</div>
      ))}</pre>
    )
  }
  return <p className="hint">未知类型</p>
}
