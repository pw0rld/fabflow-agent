import type { EngineeringReview } from '@/components/engineering-review-checklist'

/** The existing workbench owns parsing; this client never writes prices or approvals. */
export const RECOGNITION_API = '/workbench/api'
export const RECOGNITION_LAST_TASK = 'tuzhi:last-real-recognition'

export type ReadingStatus = 'not_started' | 'starting' | 'running' | 'unknown' | 'stopping' | 'completed' | 'failed' | 'stopped' | 'waiting'
export type Evidence = { reference: string; page: number | null; region: string }
export type DrawingObservation = {
  name: string; label?: string; source_name?: string; raw_text: string; raw_unit?: string | null
  status: 'observed' | 'unit_unknown' | 'conflict' | 'unreadable'; evidence: Evidence; reason?: string | null
  source_label?: string | null; classification_note?: string
}
export type DrawingCandidate = {
  name: string; label: string; value: unknown; unit?: string | null; raw_value?: unknown
  status: 'pending_review'; evidence: Evidence; reason?: string | null
}
export type DrawingHotspot = { id?: string; label: string; polygon: number[][] }
export type DrawingView = {
  id: string; label: string; reference: string; page: number; image_url: string
  bbox: [number, number, number, number]; hotspots: DrawingHotspot[]
  provenance: 'source' | 'ai' | 'prepared'; width?: number; height?: number
}
export type RecognizedPart = {
  key: string; name: string; part_number: string | null; version: string | null
  status: 'pending_review'; evidence: Evidence[]; observations: DrawingObservation[]
  bom?: { kind: 'bom_item'; item_no: number; part_number: string | null; part_numbers: string[]
    description: string | null; descriptions: string[]; quantity: number | null
    quantity_scope: 'source_bom_row'; status: string; identity_status: string; quantity_status: string
    evidence: Evidence[]; warnings: string[] }
  visualization?: { status: 'source' | 'located'; views: DrawingView[]; reason?: string
    schematic?: { kind: 'prepared-example'; template: 'sealed-box-v1' } }
}
export type DrawingAttachment = { id: string; name: string; size: number; mime: string; url: string }
export type ReadingRun = {
  id: string; status: ReadingStatus; error?: string | null; started_at?: string | null
  progress?: { stage?: string; label: string; events: { label: string; at?: string | number | null }[]
    retry?: { attempt: number; delay_ms: number } }
}
export type RecognitionTask = {
  id: string; revision: number; inquiry: string; simulated: boolean; attachments: DrawingAttachment[]
  run: ReadingRun | null; recognition?: {
    run_id?: string | null; status: ReadingStatus; available: boolean; partial: boolean
    candidates: DrawingCandidate[]; observations: DrawingObservation[]; parts: RecognizedPart[]
    parts_supported: boolean; warnings: string[]; questions: string[]; source_current: boolean
    engineering_review?: EngineeringReview
  }
}
export type RecognitionTaskSummary = {
  id: string; recognition_name?: string; recognition_available?: boolean; attachment_count: number
  updated_at: string; run?: ReadingRun | null
}

const identifier = /^[A-Za-z0-9_-]{1,200}$/
export function validTaskId(value: unknown): value is string { return typeof value === 'string' && identifier.test(value) }
export function isRecognitionRunning(task: RecognitionTask | null) {
  return Boolean(task?.run && ['starting', 'running', 'unknown', 'stopping'].includes(task.run.status))
}
export function readingStatusLabel(status?: ReadingStatus) {
  return ({ not_started: '等待开始识别', starting: '正在准备图纸', running: 'AI 正在识别图纸', unknown: '正在确认后台状态', stopping: '正在停止识别', completed: '识别已完成', failed: '本轮识别未完成', stopped: '识别已停止', waiting: '需要补充清晰图纸' })[status || 'not_started']
}
export class RecognitionApiError extends Error {
  constructor(message: string, public readonly status: number = 0, public readonly code = '') { super(message); this.name = 'RecognitionApiError' }
}
function record(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value) }
function taskResult(value: unknown): RecognitionTask {
  const data = record(value) && record(value.task) ? value.task : value
  if (!record(data) || !validTaskId(data.id) || !Number.isInteger(data.revision) || !Array.isArray(data.attachments)) {
    throw new RecognitionApiError('服务器返回的识别记录格式不完整，请刷新后重试。')
  }
  return data as unknown as RecognitionTask
}
async function request(path: string, options: RequestInit = {}): Promise<unknown> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 45_000)
  try {
    const headers = new Headers(options.headers)
    headers.set('X-Workbench-Request', '1')
    if (!(options.body instanceof Blob)) headers.set('Content-Type', 'application/json')
    const response = await fetch(RECOGNITION_API + path, { ...options, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal })
    let data: unknown
    try { data = await response.json() } catch { throw new RecognitionApiError(`服务暂未返回有效结果（${response.status}），已上传资料会保留。`, response.status) }
    if (!response.ok) {
      const error = record(data) && record(data.error) ? data.error : {}
      throw new RecognitionApiError(typeof error.message === 'string' ? error.message : `请求未完成（${response.status}）`, response.status, typeof error.code === 'string' ? error.code : '')
    }
    return data
  } catch (error) {
    if (error instanceof RecognitionApiError) throw error
    if (error instanceof Error && error.name === 'AbortError') throw new RecognitionApiError('连接等待超时，后台识别及已上传资料会保留，请刷新状态后继续。')
    throw new RecognitionApiError('网络暂时无法连接，已提交的资料会保留，请稍后重试。')
  } finally { clearTimeout(timer) }
}
function taskPath(id: string) {
  if (!validTaskId(id)) throw new RecognitionApiError('识别记录编号无效。')
  return '/tasks/' + encodeURIComponent(id)
}
export const recognitionApi = {
  async get(id: string) { return taskResult(await request(taskPath(id))) },
  async list(): Promise<RecognitionTaskSummary[]> {
    const result = await request('/tasks')
    return record(result) && Array.isArray(result.tasks) ? result.tasks.filter((item): item is RecognitionTaskSummary => record(item) && validTaskId(item.id)) : []
  },
  async create(body: { event_id: string; inquiry: string; simulated: false }) { return taskResult(await request('/tasks', { method: 'POST', body: JSON.stringify(body) })) },
  async upload(id: string, file: File, eventId: string, revision: number) {
    return taskResult(await request(taskPath(id) + '/attachments?name=' + encodeURIComponent(file.name), { method: 'POST', body: file, headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Event-Id': eventId, 'X-Expected-Revision': String(revision) } }))
  },
  async read(id: string, body: { event_id: string; expected_revision: number }) { return taskResult(await request(taskPath(id) + '/read', { method: 'POST', body: JSON.stringify(body) })) },
  async stop(id: string, body: { event_id: string; expected_revision: number }) { return taskResult(await request(taskPath(id) + '/read/stop', { method: 'POST', body: JSON.stringify(body) })) },
}

/** Source URLs may reference only this task's actual attachment or current cached PDF page. */
export function safeSourceUrl(task: RecognitionTask, raw: unknown, reference?: string, page?: number): string | null {
  if (typeof raw !== 'string' || !raw.startsWith('/workbench/') || raw.startsWith('//') || /[\\\u0000-\u0020]/.test(raw)) return null
  try {
    const parsed = new URL(raw, 'https://workbench.invalid')
    if (parsed.origin !== 'https://workbench.invalid' || parsed.hash) return null
    const files = task.attachments.filter(file => !reference || file.id === reference)
    for (const file of files) {
      if (!identifier.test(file.id)) continue
      const base = '/workbench/api/tasks/' + encodeURIComponent(task.id)
      if (parsed.pathname === base + '/attachments/' + encodeURIComponent(file.id) && !parsed.search) return raw
      if (!/\.pdf$/i.test(file.name) && file.mime !== 'application/pdf') continue
      const prefix = base + '/reading-pages/' + encodeURIComponent(file.id) + '/'
      if (!parsed.pathname.startsWith(prefix)) continue
      const result = /^(\d+)\.png$/.exec(parsed.pathname.slice(prefix.length))
      const actualPage = result ? Number(result[1]) : 0
      const runId = task.recognition?.run_id
      if (actualPage < 1 || actualPage > 10_000 || (page !== undefined && actualPage !== page) || !runId || parsed.searchParams.get('run_id') !== runId || [...parsed.searchParams.keys()].some(key => key !== 'run_id') || parsed.searchParams.getAll('run_id').length !== 1) continue
      return raw
    }
  } catch { /* malformed or external sources have no clickable target */ }
  return null
}
export function attachmentUrl(task: RecognitionTask, file: DrawingAttachment) { return safeSourceUrl(task, file.url, file.id) }
export function validDrawingView(task: RecognitionTask, view: DrawingView, part: RecognizedPart): boolean {
  const box = view?.bbox
  return Boolean(part.visualization && ['source', 'located'].includes(part.visualization.status)
    && typeof view?.image_url === 'string'
    && ['source', 'ai', 'prepared'].includes(view?.provenance)
    && part.evidence.some(e => e.reference === view.reference && e.page === view.page)
    && Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
    && box[0] >= 0 && box[1] >= 0 && box[2] > 0 && box[3] > 0 && box[0] + box[2] <= 1000 && box[1] + box[3] <= 1000
    && (view.image_url.includes('/reading-pages/') || view.page === 1 && Boolean(task.attachments.find(file => file.id === view.reference && /^image\/(png|jpeg|webp|gif|bmp)$/.test(file.mime))))
    && safeSourceUrl(task, view.image_url, view.reference, view.page))
}
