import type { AdminUser, AnalysisResult, ApiSettingsPayload, AuditLog, ConfigStatus, DashboardData, Dataset, DatasetQuality, KnowledgeItem, SessionDetail, SessionSummary, StreamCallbacks } from './types'

const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'
const CHUNK_UPLOAD_THRESHOLD = 8 * 1024 * 1024
const CHUNK_SIZE = 5 * 1024 * 1024

function normalizeDetail(detail: unknown): string {
  if (Array.isArray(detail)) return detail.map((e: any) => e?.msg || e?.message || JSON.stringify(e)).join('; ')
  if (detail && typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    const value = obj.detail || obj.message || obj.error
    if (value) return normalizeDetail(value)
    return JSON.stringify(obj)
  }
  return typeof detail === 'string' ? detail.trim() : ''
}

function fallbackStatusMessage(status: number, fallback = '请求失败') {
  const map: Record<number, string> = {
    400: '请求参数不正确，请检查问题、数据源或上传文件。',
    401: '登录已失效，请重新登录后再试。',
    403: '当前账号没有权限执行该操作或访问该数据源。',
    404: '接口或资源不存在，请确认后端服务版本是否最新。',
    413: '上传文件过大，请压缩或拆分文件后重试。',
    422: '请求格式校验失败，请检查输入内容是否为空或格式不正确。',
    429: '请求过于频繁，服务正在限流，请稍后再试。',
    500: '后端服务内部错误，请查看后端控制台日志。',
    502: '后端网关/代理连接失败，请检查模型服务、代理或本地服务是否可用。',
    503: '后端服务暂不可用，请确认数据库、Qdrant、模型 API 是否已启动。',
    504: '请求超时，请缩小分析范围或稍后重试。',
  }
  return `${map[status] || fallback}（HTTP ${status}）`
}

async function readResponseError(response: Response, fallback = '请求失败') {
  const text = await response.text().catch(() => '')
  if (text) {
    try {
      const body = JSON.parse(text)
      const detail = normalizeDetail(body.detail || body.message || body.error || body)
      if (detail) return detail
    } catch {
      const plain = text.trim()
      if (plain) return plain
    }
  }
  return fallbackStatusMessage(response.status, fallback)
}

function parseXhrError(status: number, responseText: string, fallback = '请求失败') {
  if (responseText) {
    try {
      const body = JSON.parse(responseText)
      const detail = normalizeDetail(body.detail || body.message || body.error || body)
      if (detail) return detail
    } catch {
      const plain = responseText.trim()
      if (plain) return plain
    }
  }
  return fallbackStatusMessage(status, fallback)
}

function uploadErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || '')
  if (message.includes('429') || message.includes('请求过于频繁') || message.includes('Too Many Requests')) {
    return '上传请求过于频繁，后端正在限流。请稍等片刻后继续上传；已上传分片会自动断点续传。'
  }
  if (message.includes('网络连接失败')) {
    return '上传请求无法到达后端，请确认后端服务已启动、浏览器代理没有拦截 localhost，或刷新页面后重试。'
  }
  return message || '上传失败，请稍后重试。'
}

function networkErrorMessage(err: unknown, fallback = '网络连接失败') {
  const message = err instanceof Error ? err.message : String(err || '')
  if (message.includes('Failed to fetch')) {
    return `${fallback}：浏览器无法连接后端。请确认后端已启动、端口正确、代理没有拦截 localhost。`
  }
  if (message.includes('NetworkError')) {
    return `${fallback}：网络连接被中断，请检查后端服务、代理和防火墙。`
  }
  return message || fallback
}

export type UploadProgress = {
  percent: number
  status: string
  uploadedBytes: number
  totalBytes: number
}

export type MySQLConnectionPayload = {
  host: string
  port: number
  username: string
  password: string
  database?: string
  table?: string
  connect_timeout?: number
  read_timeout?: number
  ssl_enabled?: boolean
  ssl_ca?: string
  ssl_cert?: string
  ssl_key?: string
  ssh_enabled?: boolean
  ssh_host?: string
  ssh_port?: number
  ssh_username?: string
  ssh_password?: string
  ssh_pkey?: string
  ssh_private_key_passphrase?: string
}

export type MySQLSchema = {
  databases: string[]
  tables: Array<{ name: string; type: string; rows: number }>
  columns: Array<{ name: string; data_type: string; column_type: string; nullable: boolean; key: string; comment: string }>
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, credentials: 'include' })
  } catch (err) {
    throw new Error(networkErrorMessage(err, '请求后端失败'))
  }
  if (!response.ok) {
    throw new Error(await readResponseError(response))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function xhrFormRequest<T>(
  path: string,
  form: FormData,
  onProgress?: (progress: UploadProgress) => void,
  progressBase = 0,
  progressTotal = 1,
  status = '正在上传文件',
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}${path}`)
    xhr.withCredentials = true
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return
      const uploadedBytes = progressBase + event.loaded
      const totalBytes = progressTotal
      onProgress?.({
        percent: Math.min(99, Math.round((uploadedBytes / totalBytes) * 100)),
        status,
        uploadedBytes,
        totalBytes,
      })
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.responseText ? JSON.parse(xhr.responseText) as T : undefined as T)
        return
      }
      reject(new Error(parseXhrError(xhr.status, xhr.responseText)))
    }
    xhr.onerror = () => reject(new Error('网络连接失败：上传请求无法到达后端，请确认后端服务已启动且代理没有拦截 localhost。'))
    xhr.ontimeout = () => reject(new Error('请求超时：文件上传或后端处理时间过长，请缩小文件或稍后重试。'))
    xhr.send(form)
  })
}

function uploadSignature(file: File) {
  return `dataagent-upload:${file.name}:${file.size}:${file.lastModified}`
}

function newUploadId() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function chunkSizeAt(file: File, index: number) {
  return Math.max(0, Math.min(CHUNK_SIZE, file.size - index * CHUNK_SIZE))
}

async function uploadSmallFile(
  file: File,
  name: string,
  description: string,
  onProgress?: (progress: UploadProgress) => void,
) {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  form.append('description', description)
  let result: Dataset
  try {
    result = await xhrFormRequest<Dataset>('/datasets/upload', form, onProgress, 0, file.size || 1, '正在上传文件')
  } catch (err) {
    throw new Error(uploadErrorMessage(err))
  }
  onProgress?.({ percent: 100, status: '上传完成，已生成数据集', uploadedBytes: file.size, totalBytes: file.size })
  return result
}

async function uploadChunkedFile(
  file: File,
  name: string,
  description: string,
  onProgress?: (progress: UploadProgress) => void,
) {
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE)
  const key = uploadSignature(file)
  const uploadId = localStorage.getItem(key) || newUploadId()
  localStorage.setItem(key, uploadId)

  const status = await request<{ received_chunks: number[] }>(`/datasets/upload/chunks/${encodeURIComponent(uploadId)}`).catch(() => ({ received_chunks: [] }))
  const received = new Set(status.received_chunks || [])
  let uploadedBytes = [...received].reduce((sum, index) => sum + chunkSizeAt(file, index), 0)
  onProgress?.({
    percent: Math.round((uploadedBytes / file.size) * 100),
    status: received.size ? `继续上传：已恢复 ${received.size}/${totalChunks} 个分片` : `准备分片上传：共 ${totalChunks} 个分片`,
    uploadedBytes,
    totalBytes: file.size,
  })

  for (let index = 0; index < totalChunks; index += 1) {
    if (received.has(index)) continue
    const start = index * CHUNK_SIZE
    const chunk = file.slice(start, Math.min(file.size, start + CHUNK_SIZE))
    const form = new FormData()
    form.append('file', chunk, file.name)
    form.append('upload_id', uploadId)
    form.append('chunk_index', String(index))
    form.append('total_chunks', String(totalChunks))
    form.append('total_size', String(file.size))
    form.append('filename', file.name)
    try {
      await xhrFormRequest(
        '/datasets/upload/chunk',
        form,
        onProgress,
        uploadedBytes,
        file.size,
        `正在上传分片 ${index + 1}/${totalChunks}`,
      )
    } catch (err) {
      throw new Error(uploadErrorMessage(err))
    }
    uploadedBytes += chunk.size
    received.add(index)
  }

  onProgress?.({ percent: 99, status: '正在合并分片并生成数据表', uploadedBytes: file.size, totalBytes: file.size })
  const form = new FormData()
  form.append('upload_id', uploadId)
  form.append('filename', file.name)
  form.append('total_chunks', String(totalChunks))
  form.append('total_size', String(file.size))
  if (name) form.append('name', name)
  form.append('description', description)
  const result = await request<Dataset>('/datasets/upload/complete', { method: 'POST', body: form })
  localStorage.removeItem(key)
  onProgress?.({ percent: 100, status: '上传完成，已生成数据集', uploadedBytes: file.size, totalBytes: file.size })
  return result
}

export const api = {
  login: (username: string, password: string) =>
    request<{ admin: AdminUser }>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  me: () => request<{ admin: AdminUser }>('/auth/me'),
  admins: () => request<AdminUser[]>('/auth/admins'),
  createAdmin: (payload: { username: string; password: string; role?: string; dataset_ids?: number[] }) =>
    request<AdminUser>('/auth/admins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateAdmin: (id: number, payload: { role: string; dataset_ids: number[] }) =>
    request<AdminUser>(`/auth/admins/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  deleteAdmin: (id: number) => request<void>(`/auth/admins/${id}`, { method: 'DELETE' }),
  dashboard: () => request<DashboardData>('/dashboard'),
  config: () => request<ConfigStatus>('/config/status'),
  saveApiSettings: (payload: ApiSettingsPayload) =>
    request<ConfigStatus>('/config/api', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  datasets: () => request<Dataset[]>('/datasets'),
  dataset: (id: number) => request<Dataset>(`/datasets/${id}`),
  updateDataset: (id: number, payload: { name: string; description: string }) =>
    request<Dataset>(`/datasets/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateDatasetColumn: (id: number, column: string, description: string) =>
    request<Dataset>(`/datasets/${id}/columns/${encodeURIComponent(column)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description }),
    }),
  deleteDataset: (id: number) => request<void>(`/datasets/${id}`, { method: 'DELETE' }),
  datasetQuality: (id: number) => request<DatasetQuality>(`/datasets/${id}/quality`),
  importMysql: (payload: {
    host: string
    port: number
    username: string
    password: string
    database: string
    table: string
    name?: string
    description?: string
    limit?: number
    connect_timeout?: number
    read_timeout?: number
    ssl_enabled?: boolean
    ssl_ca?: string
    ssl_cert?: string
    ssl_key?: string
    ssh_enabled?: boolean
    ssh_host?: string
    ssh_port?: number
    ssh_username?: string
    ssh_password?: string
    ssh_pkey?: string
    ssh_private_key_passphrase?: string
  }) =>
    request<Dataset>('/datasets/mysql/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  testMysql: (payload: MySQLConnectionPayload) =>
    request<Record<string, unknown>>('/datasets/mysql/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  mysqlSchema: (payload: MySQLConnectionPayload) =>
    request<MySQLSchema>('/datasets/mysql/schema', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  knowledge: (datasetId?: number) => request<KnowledgeItem[]>(`/knowledge${datasetId ? `?dataset_id=${datasetId}` : ''}`),
  sessions: () => request<SessionSummary[]>('/sessions'),
  session: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: 'DELETE' }),
  analyze: (question: string, datasetId?: number, sessionId?: string) =>
    request<AnalysisResult>('/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, dataset_id: datasetId, session_id: sessionId }),
    }),
  analyzeStream: (
    question: string,
    datasetId: number | undefined,
    sessionId: string | undefined,
    callbacks: StreamCallbacks,
  ) => {
    const controller = new AbortController()
    let aborted = false
    let doneCalled = false
    let resultReceived = false

    const run = async () => {
      try {
        const response = await fetch(`${API_BASE}/agent/chat/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question, dataset_id: datasetId, session_id: sessionId }),
          credentials: 'include',
          signal: controller.signal,
        })

        if (!response.ok) {
          callbacks.onError(await readResponseError(response, '智能分析请求失败'))
          return
        }

        const reader = response.body?.getReader()
        if (!reader) { callbacks.onError('浏览器不支持流式读取'); return }

        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })

          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const event = JSON.parse(line.slice(6))
                switch (event.type) {
                  case 'plan':
                    callbacks.onPlan(event.steps || [], event.intent || '', event.answer_type || '')
                    break
                  case 'step':
                    callbacks.onStep(event.step_id || 0, event.title || '', event.status || 'pending', event.detail)
                    break
                  case 'thinking':
                    callbacks.onThinking(event.content || '')
                    break
                  case 'result':
                    resultReceived = true
                    callbacks.onResult(event.data)
                    break
                  case 'done':
                    doneCalled = true
                    if (event.error || event.message) {
                      callbacks.onError(normalizeDetail(event.error || event.message) || '分析失败：后端未返回具体错误')
                      return
                    }
                    if (!resultReceived) {
                      callbacks.onError('分析流程提前结束：后端没有返回分析结果。请重试，或检查后端控制台日志。')
                      return
                    }
                    callbacks.onDone()
                    break
                  case 'error':
                    doneCalled = true
                    callbacks.onError(normalizeDetail(event.message || event.error) || '分析失败：后端未返回具体错误')
                    return
                    break
                }
              } catch {
                // skip malformed events
              }
            }
          }
        }

        // Process any remaining data in buffer after stream ends
        if (!doneCalled && buffer.trim()) {
          const line = buffer.trim()
          if (line.startsWith('data: ')) {
            try {
              const event = JSON.parse(line.slice(6))
              if (event.type === 'done') {
                doneCalled = true
                if (event.error || event.message) {
                  callbacks.onError(normalizeDetail(event.error || event.message) || '分析失败：后端未返回具体错误')
                } else if (!resultReceived) {
                  callbacks.onError('分析流程提前结束：后端没有返回分析结果。请重试，或检查后端控制台日志。')
                } else {
                  callbacks.onDone()
                }
              } else if (event.type === 'error') {
                doneCalled = true
                callbacks.onError(normalizeDetail(event.message || event.error) || '分析失败：后端未返回具体错误')
              }
            } catch {
              // skip malformed final event
            }
          }
        }

        // Guard: only a stream that already delivered a result may finish without an explicit done event.
        if (!aborted && !doneCalled) {
          if (resultReceived) {
            callbacks.onDone()
          } else {
            callbacks.onError('分析流已中断：没有收到分析结果。请检查后端是否报错、网络是否断开，或稍后重试。')
          }
        }
      } catch (err: any) {
        if (err.name === 'AbortError') {
          aborted = true
        } else {
          callbacks.onError(networkErrorMessage(err, '智能分析请求失败'))
        }
      }
    }

    run()
    // Return a direct cancel function so streamCancel?.() works correctly
    const cancel = () => {
      aborted = true
      controller.abort()
    }
    return cancel
  },
  upload: (file: File, name: string, description: string, onProgress?: (progress: UploadProgress) => void) =>
    file.size > CHUNK_UPLOAD_THRESHOLD
      ? uploadChunkedFile(file, name, description, onProgress)
      : uploadSmallFile(file, name, description, onProgress),
  createKnowledge: (payload: { title: string; content: string; category: string; dataset_id?: number }) =>
    request<KnowledgeItem>('/knowledge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  updateKnowledge: (id: number, payload: { title: string; content: string; category: string; dataset_id?: number }) =>
    request<KnowledgeItem>(`/knowledge/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  uploadKnowledge: (file: File, title: string, category: string, datasetId?: number) => {
    const form = new FormData()
    form.append('file', file)
    if (title) form.append('title', title)
    form.append('category', category)
    if (datasetId) form.append('dataset_id', String(datasetId))
    return request<KnowledgeItem[]>('/knowledge/upload', { method: 'POST', body: form })
  },
  deleteKnowledge: (id: number) => request<void>(`/knowledge/${id}`, { method: 'DELETE' }),
  reindexKnowledge: () => request<Record<string, unknown>>('/knowledge/reindex', { method: 'POST' }),
  analyzeFile: (file: File, question: string, datasetId?: number, sessionId?: string) => {
    const form = new FormData()
    form.append('file', file)
    if (question) form.append('question', question)
    if (datasetId) form.append('dataset_id', String(datasetId))
    if (sessionId) form.append('session_id', sessionId)
    return request<AnalysisResult>('/agent/chat/with-file', { method: 'POST', body: form })
  },
  planReport: (question: string, datasetId?: number) => {
    const form = new FormData()
    form.append('question', question)
    if (datasetId) form.append('dataset_id', String(datasetId))
    return request<{ phase: string; plan?: any; persona?: string; clarification?: any }>('/agent/report/plan', { method: 'POST', body: form })
  },
  auditLogs: (query = '') => request<AuditLog[]>(`/audit/logs${query}`),
  auditExportUrl: (query = '') => `${API_BASE}/audit/logs/export.xlsx${query}`,
  downloadReportPdf: async (payload: { title: string; sections: any[]; executive_summary?: string; data_source?: string; sql_list?: string[] }) => {
    const resp = await fetch(`${API_BASE}/agent/report/pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      credentials: 'include',
    })
    if (!resp.ok) throw new Error('PDF 生成失败')
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = '数据分析报告.pdf'; a.click()
    URL.revokeObjectURL(url)
  },
  reportUrl: (
    sessionId: string,
    format: 'html' | 'docx' | 'pdf' | 'md' = 'html',
    chartOptions?: { sections: Array<{ index: number; id?: string; type: string }> },
  ) => {
    const base = `${API_BASE}/reports/${sessionId}.${format}`
    if (!chartOptions?.sections?.length) return base
    return `${base}?chart_options=${encodeURIComponent(JSON.stringify(chartOptions))}`
  },
}
