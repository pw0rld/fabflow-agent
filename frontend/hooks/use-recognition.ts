'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { isRecognitionRunning, recognitionApi, RecognitionApiError, RECOGNITION_LAST_TASK, type RecognitionTask } from '@/lib/recognition-api'

type Mutation = { event_id: string; expected_revision: number }
type Upload = { file: File; pending?: Mutation; completed: boolean }
type Submission = { key: string; inquiry: string; event: string; taskId?: string; uploads: Upload[] }
const eventId = () => 'v0-' + crypto.randomUUID()
const message = (error: unknown) => error instanceof Error ? error.message : '操作暂未完成，请稍后重试。'
class SupersededOperation extends Error {}

export function useRecognition() {
  const [task, setTask] = useState<RecognitionTask | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current = useRef<RecognitionTask | null>(null)
  const alive = useRef(true)
  const generation = useRef(0)
  const locked = useRef(false)
  const submission = useRef<Submission | null>(null)
  const mutations = useRef(new Map<string, Mutation>())
  const commit = useCallback((value: RecognitionTask) => {
    // A GET can acknowledge a POST whose response was lost. Once the server
    // exposes that exact event as its run, a later explicit reread needs a new ID.
    const readKey = value.id + ':read'
    const pendingRead = mutations.current.get(readKey)
    if (pendingRead && value.run?.id === pendingRead.event_id) mutations.current.delete(readKey)
    current.current = value
    if (alive.current) setTask(value)
    try { localStorage.setItem(RECOGNITION_LAST_TASK, value.id) } catch { /* private storage may be disabled */ }
    return value
  }, [])
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++ } }, [])

  const load = useCallback(async (id: string) => {
    const token = ++generation.current
    if (current.current?.id !== id) {
      current.current = null
      if (alive.current) setTask(null)
    }
    if (alive.current) { setLoading(true); setError('') }
    try { const result = await recognitionApi.get(id); if (alive.current && token === generation.current) commit(result) }
    catch (failure) { if (alive.current && token === generation.current) setError(message(failure)) }
    finally { if (alive.current && token === generation.current) setLoading(false) }
  }, [commit])

  // GET only: returning to the page resumes observation of the same server run.
  useEffect(() => {
    if (!task || !isRecognitionRunning(task)) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const id = task.id
    const poll = async () => {
      if (cancelled) return
      if (locked.current) { timer = setTimeout(poll, 2500); return }
      const token = generation.current
      try {
        const result = await recognitionApi.get(id)
        if (!cancelled && alive.current && token === generation.current && current.current?.id === id) { commit(result); setError('') }
      } catch (failure) {
        if (!cancelled && alive.current && token === generation.current) setError('暂时无法刷新状态，后台识别会继续。' + message(failure))
      }
      if (!cancelled) timer = setTimeout(poll, 2500)
    }
    timer = setTimeout(poll, 2500)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [task?.id, task?.run?.status, commit])

  const checkedCommit = useCallback((value: RecognitionTask, token: number) => {
    if (!alive.current || token !== generation.current) throw new SupersededOperation()
    return commit(value)
  }, [commit])
  const mutate = useCallback(async (value: RecognitionTask, operation: 'read' | 'stop', token: number) => {
    if (!alive.current || token !== generation.current) throw new SupersededOperation()
    const key = value.id + ':' + operation
    let pending = mutations.current.get(key)
    if (!pending) { pending = { event_id: eventId(), expected_revision: value.revision }; mutations.current.set(key, pending) }
    try {
      const result = await recognitionApi[operation](value.id, pending)
      mutations.current.delete(key)
      return checkedCommit(result, token)
    } catch (failure) {
      if (failure instanceof RecognitionApiError && failure.status > 0 && failure.status < 500) mutations.current.delete(key)
      throw failure
    }
  }, [checkedCommit])

  const start = useCallback(async (files: File[], note: string, taskId?: string): Promise<RecognitionTask | null> => {
    if (locked.current) return null
    locked.current = true
    const token = ++generation.current
    if (alive.current) { setBusy(true); setError('') }
    try {
      if (!files.length && !taskId) throw new Error('请先选择待识别的图纸。')
      for (const file of files) if (file.size > 25 * 1024 * 1024) throw new Error(file.name + ' 超过 25 MB，请缩小后上传。')
      const key = JSON.stringify([taskId || '', note.trim(), ...files.map(file => [file.name, file.size, file.lastModified])])
      if (!submission.current || submission.current.key !== key) submission.current = {
        key,
        inquiry: note.trim() || '系统识别任务：解析本次上传图纸。操作者未提供额外询价文字。',
        event: eventId(), taskId, uploads: files.map(file => ({ file, completed: false })),
      }
      const draft = submission.current
      let value = draft.taskId ? checkedCommit(await recognitionApi.get(draft.taskId), token) : checkedCommit(await recognitionApi.create({ event_id: draft.event, inquiry: draft.inquiry, simulated: false }), token)
      draft.taskId = value.id
      if (isRecognitionRunning(value)) return value
      for (const upload of draft.uploads) {
        if (upload.completed) continue
        if (!upload.pending) upload.pending = { event_id: eventId(), expected_revision: value.revision }
        try { value = checkedCommit(await recognitionApi.upload(value.id, upload.file, upload.pending.event_id, upload.pending.expected_revision), token); upload.completed = true }
        catch (failure) {
          if (failure instanceof RecognitionApiError && failure.status > 0 && failure.status < 500) upload.pending = undefined
          throw failure
        }
      }
      value = checkedCommit(await recognitionApi.get(value.id), token)
      if (!value.attachments.length) throw new Error('请先上传图纸。')
      return isRecognitionRunning(value) ? value : await mutate(value, 'read', token)
    } catch (failure) { if (alive.current && token === generation.current && !(failure instanceof SupersededOperation)) setError(message(failure)); return null }
    finally { locked.current = false; if (alive.current) setBusy(false) }
  }, [checkedCommit, mutate])

  const retry = useCallback(async (): Promise<RecognitionTask | null> => {
    if (locked.current || !current.current) return null
    locked.current = true
    const token = ++generation.current
    if (alive.current) { setBusy(true); setError('') }
    try {
      const value = checkedCommit(await recognitionApi.get(current.current.id), token)
      return isRecognitionRunning(value) ? value : await mutate(value, 'read', token)
    } catch (failure) { if (alive.current && token === generation.current && !(failure instanceof SupersededOperation)) setError(message(failure)); return null }
    finally { locked.current = false; if (alive.current) setBusy(false) }
  }, [checkedCommit, mutate])
  const stop = useCallback(async () => {
    if (locked.current || !current.current) return
    locked.current = true
    const token = ++generation.current
    if (alive.current) { setBusy(true); setError('') }
    try { const value = checkedCommit(await recognitionApi.get(current.current.id), token); if (isRecognitionRunning(value)) await mutate(value, 'stop', token) }
    catch (failure) { if (alive.current && token === generation.current && !(failure instanceof SupersededOperation)) setError(message(failure)) }
    finally { locked.current = false; if (alive.current) setBusy(false) }
  }, [checkedCommit, mutate])
  const reset = useCallback(() => {
    // Reset affects the browser only; an existing server run continues in the background.
    generation.current++; current.current = null; submission.current = null
    if (alive.current) { setTask(null); setError(''); setLoading(false) }
    try { localStorage.removeItem(RECOGNITION_LAST_TASK) } catch { /* optional persistence */ }
  }, [])
  return { task, loading, busy, error, load, start, retry, stop, reset }
}
