<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from './components/AppIcon.vue'
import { api } from './api'
import type { AdminUser, AnalysisResult, ChatMessage, ConfigStatus, DashboardData, Dataset, KnowledgeItem, SessionSummary, ThinkingStep, ViewName } from './types'
import OverviewView from './views/OverviewView.vue'
import AnalystView from './views/AnalystView.vue'
import DatasetsView from './views/DatasetsView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import AccountsView from './views/AccountsView.vue'
import SettingsView from './views/SettingsView.vue'
import AuditView from './views/AuditView.vue'

const activeView = ref<ViewName>('overview')
const dashboard = ref<DashboardData | null>(null)
const datasets = ref<Dataset[]>([])
const config = ref<ConfigStatus | null>(null)
const knowledge = ref<KnowledgeItem[]>([])
const sessions = ref<SessionSummary[]>([])
const chatMessages = ref<ChatMessage[]>([])
const selectedDatasetId = ref<number | undefined>()
const selectedDataset = ref<Dataset | null>(null)
const result = ref<AnalysisResult | null>(null)
const error = ref('')
const loading = ref(false)
const sessionId = ref<string>()
const thinkingSteps = ref<ThinkingStep[]>([])
const thinkingText = ref('')
const thinkingCollapsed = ref(false)
const streamCancel = ref<(() => void) | null>(null)
const currentAdmin = ref<AdminUser | null>(null)
const admins = ref<AdminUser[]>([])
const authChecked = ref(false)
const loginForm = ref({ username: '', password: '' })
const loginLoading = ref(false)
const sidebarCollapsed = ref(false)
type ConfirmDialogOptions = {
  title: string
  message: string
  detail?: string
  confirmText?: string
  cancelText?: string
  variant?: 'danger' | 'default'
}
const confirmDialog = ref({
  open: false,
  title: '',
  message: '',
  detail: '',
  confirmText: '确定',
  cancelText: '取消',
  variant: 'danger' as 'danger' | 'default',
})
let confirmResolver: ((confirmed: boolean) => void) | null = null

const navItems: Array<{ id: ViewName; label: string; icon: string }> = [
  { id: 'overview', label: '工作台', icon: 'home' },
  { id: 'analyst', label: '智能分析', icon: 'spark' },
  { id: 'datasets', label: '数据源', icon: 'database' },
  { id: 'knowledge', label: '业务知识库', icon: 'book' },
  { id: 'audit', label: '审计日志', icon: 'chart' },
  { id: 'accounts', label: '账户管理', icon: 'settings' },
  { id: 'settings', label: '系统配置', icon: 'settings' },
]
const pageTitle = computed(() => navItems.find(i => i.id === activeView.value)?.label || 'DataAgent')
const examples = ['统计各地区销售额', '按月份展示销售额趋势', '查询投诉率最高的区域', '分析华东地区转化率']

function showConfirm(options: ConfirmDialogOptions) {
  confirmDialog.value = {
    open: true,
    title: options.title,
    message: options.message,
    detail: options.detail || '',
    confirmText: options.confirmText || '确定',
    cancelText: options.cancelText || '取消',
    variant: options.variant || 'danger',
  }
  return new Promise<boolean>((resolve) => {
    confirmResolver = resolve
  })
}

function closeConfirm(confirmed: boolean) {
  confirmDialog.value.open = false
  confirmResolver?.(confirmed)
  confirmResolver = null
}

function cleanInsightText(text: string) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .replace(/^[-•\d.、\s]+/, '')
    .trim()
}

function shortenText(text: string, max = 150) {
  const cleaned = cleanInsightText(text)
  if (cleaned.length <= max) return cleaned
  const cut = cleaned.slice(0, max)
  const sentenceEnd = Math.max(cut.lastIndexOf('。'), cut.lastIndexOf('；'), cut.lastIndexOf(';'))
  return `${(sentenceEnd > 42 ? cut.slice(0, sentenceEnd + 1) : cut).trim()}…`
}

function followupSuggestions(data: AnalysisResult) {
  const sections = data.chart_sections || []
  if (sections.length > 1) {
    const first = sections[0]?.title || '核心图表'
    return [
      `重点解释「${first}」为什么最关键`,
      '继续下钻风险点和形成原因',
      '把多组图表整理成答辩汇报话术',
    ]
  }
  if (data.execution_mode?.includes('document')) {
    return [
      '提炼适合答辩的3条核心结论',
      '继续拆解风险点和应对建议',
      '按财务、业务、技术三个维度重新总结',
    ]
  }
  if (data.answer_type === 'knowledge_qa') {
    return [
      '这个指标的计算口径是什么？',
      '它对应哪些数据字段？',
      '给一个业务场景中的使用例子',
    ]
  }
  return [
    '解释最高和最低项的原因',
    '继续按地区、产品或渠道下钻',
    '把本轮分析导出成报告',
  ]
}

function assistantReply(data: AnalysisResult) {
  const insights = (data.insights || []).map(cleanInsightText).filter(Boolean)
  const primary = insights[0] || data.message || '分析已完成。'
  const secondary = insights.find((item, index) => index > 0 && !item.startsWith('建议：'))
  const suggestions = followupSuggestions(data)
  const lines = [
    `结论：${shortenText(primary, 170)}`,
  ]
  if (secondary) lines.push(`补充判断：${shortenText(secondary, 120)}`)
  lines.push('', '你可以继续问：', ...suggestions.map((item, index) => `${index + 1}. ${item}`))
  lines.push('', `右侧已整理完整图表和依据，可点击查看该轮结果。`)
  return lines.join('\n')
}

async function loadBase() {
  try {
    const [d, ds, c, k, s] = await Promise.all([api.dashboard(), api.datasets(), api.config(), api.knowledge(), api.sessions()])
    dashboard.value = d; datasets.value = ds; config.value = c; knowledge.value = k; sessions.value = s
    selectedDatasetId.value ||= ds[0]?.id
  } catch (err: any) { error.value = err.message || '后端服务未连接' }
}

async function bootstrap() {
  try { const me = await api.me(); currentAdmin.value = me.admin; await loadBase(); admins.value = await api.admins() }
  catch { currentAdmin.value = null }
  finally { authChecked.value = true }
}

async function login() {
  if (!loginForm.value.username || !loginForm.value.password) return
  loginLoading.value = true; error.value = ''
  try { const d = await api.login(loginForm.value.username, loginForm.value.password); currentAdmin.value = d.admin; loginForm.value.password = ''; await loadBase(); admins.value = await api.admins() }
  catch (err: any) { error.value = err.message || '登录失败' }
  finally { loginLoading.value = false }
}

async function logout() {
  await api.logout().catch(() => {})
  currentAdmin.value = null; dashboard.value = null; datasets.value = []; config.value = null; knowledge.value = []; sessions.value = []; chatMessages.value = []; result.value = null; sessionId.value = undefined; activeView.value = 'overview'
}

function analyze(q: string) {
  const v = q.trim(); if (!v) return
  loading.value = true; error.value = ''; activeView.value = 'analyst'

  // Reset streaming state
  thinkingSteps.value = []
  thinkingText.value = ''
  thinkingCollapsed.value = false

  // Cancel any in-flight stream
  streamCancel.value?.()

  chatMessages.value.push({ role: 'user', content: v })

  // Add a placeholder assistant message that will be updated
  const assistantMsg: ChatMessage = { role: 'assistant', content: '', payload: null }
  chatMessages.value.push(assistantMsg)

  streamCancel.value = api.analyzeStream(v, selectedDatasetId.value, sessionId.value, {
    onPlan(steps, intent, answerType) {
      thinkingSteps.value = steps.map((title, i) => ({
        id: i + 1,
        title,
        status: 'pending' as const,
      }))
    },
    onStep(stepId, title, status, detail) {
      const existing = thinkingSteps.value.find(s => s.id === stepId)
      if (existing) {
        existing.status = status as ThinkingStep['status']
        if (detail) existing.detail = detail
      } else if (status === 'running') {
        thinkingSteps.value.push({ id: stepId, title, status: 'running', detail })
      }
    },
    onThinking(content) {
      if (thinkingText.value) {
        thinkingText.value += '\n' + content
      } else {
        thinkingText.value = content
      }
    },
    onResult(data) {
      result.value = data
      sessionId.value = data.session_id
      assistantMsg.content = assistantReply(data)
      assistantMsg.payload = data
      assistantMsg._streamed = true
    },
    async onDone() {
      if (!assistantMsg.payload) {
        chatMessages.value.pop()
        error.value = '分析流程提前结束：没有收到有效分析结果。请重试，或查看后端控制台日志。'
        loading.value = false
        return
      }
      loading.value = false
      // Auto-collapse thinking after a short delay
      setTimeout(() => { thinkingCollapsed.value = true }, 1500)
      // Refresh sessions and dashboard
      sessions.value = await api.sessions()
      dashboard.value = await api.dashboard()
    },
    onError(message) {
      // Remove the placeholder assistant message
      chatMessages.value.pop()
      error.value = message || '分析失败'
      loading.value = false
    },
  })
}

function newSession() {
  sessionId.value = undefined
  chatMessages.value = []
  result.value = null
  thinkingSteps.value = []
  thinkingText.value = ''
  thinkingCollapsed.value = false
  loading.value = false
  error.value = ''
}

async function analyzeFile(file: File, q: string) {
  loading.value = true; error.value = ''; activeView.value = 'analyst'
  thinkingSteps.value = []; thinkingText.value = ''; thinkingCollapsed.value = false
  streamCancel.value?.()

  const displayQ = q || `分析文档: ${file.name}`
  chatMessages.value.push({ role: 'user', content: `${displayQ}\n[已上传: ${file.name}]` })
  const assistantMsg: ChatMessage = { role: 'assistant', content: '', payload: null }
  chatMessages.value.push(assistantMsg)

  try {
    const data = await api.analyzeFile(file, q, selectedDatasetId.value, sessionId.value)
    result.value = data
    sessionId.value = data.session_id
    assistantMsg.content = assistantReply(data)
    assistantMsg.payload = data
    assistantMsg._streamed = true
    sessions.value = await api.sessions()
    dashboard.value = await api.dashboard()
  } catch (err: any) {
    chatMessages.value.pop()
    error.value = err.message || '文档分析失败'
  } finally {
    loading.value = false
  }
}

function toggleThinking() {
  thinkingCollapsed.value = !thinkingCollapsed.value
}

async function openSession(id: string) {
  loading.value = true; error.value = ''
  try { const d = await api.session(id); sessionId.value = d.id; selectedDatasetId.value = d.dataset_id || selectedDatasetId.value; chatMessages.value = d.messages; result.value = [...d.messages].reverse().find(m => m.role === 'assistant' && m.payload)?.payload || null; activeView.value = 'analyst' }
  catch (err: any) { error.value = err.message || '读取历史会话失败' }
  finally { loading.value = false }
}

async function deleteSession(id: string) {
  const s = sessions.value.find(i => i.id === id)
  const confirmed = await showConfirm({
    title: '删除历史对话',
    message: `确定删除「${s?.title || '该历史对话'}」吗？`,
    detail: '删除后将无法从历史对话中恢复。',
    confirmText: '确认删除',
  })
  if (!confirmed) return
  try { await api.deleteSession(id); sessions.value = sessions.value.filter(i => i.id !== id); if (sessionId.value === id) { sessionId.value = undefined; chatMessages.value = []; result.value = null }; dashboard.value = await api.dashboard() }
  catch (err: any) { error.value = err.message || '删除失败' }
}

async function inspectDataset(id: number) { activeView.value = 'datasets'; selectedDatasetId.value = id; selectedDataset.value = await api.dataset(id) }

async function doUpload(file: File, name: string, desc: string) {
  try { const created = await api.upload(file, name, desc); datasets.value = await api.datasets(); selectedDatasetId.value = created.id; selectedDataset.value = created; dashboard.value = await api.dashboard() }
  catch (err: any) { error.value = err.message || '上传失败' }
}

async function deleteDataset(id: number) {
  const item = datasets.value.find(ds => ds.id === id)
  const name = item?.name || '该数据集'
  const confirmed = await showConfirm({
    title: '删除数据集',
    message: `确定删除「${name}」吗？`,
    detail: '删除后会同时清理该数据集的物理表、字段元数据、权限记录和关联知识片段。此操作不可撤销。',
    confirmText: '确认删除',
  })
  if (!confirmed) return
  try {
    await api.deleteDataset(id)
    datasets.value = await api.datasets()
    currentAdmin.value = currentAdmin.value
      ? { ...currentAdmin.value, dataset_permissions: (currentAdmin.value.dataset_permissions || []).filter(item => item !== id) }
      : currentAdmin.value
    if (selectedDatasetId.value === id || selectedDataset?.value?.id === id) {
      selectedDatasetId.value = datasets.value[0]?.id
      selectedDataset.value = selectedDatasetId.value ? await api.dataset(selectedDatasetId.value) : null
    }
    knowledge.value = await api.knowledge()
    dashboard.value = await api.dashboard()
    config.value = await api.config()
  } catch (err: any) {
    error.value = err.message || '删除数据集失败'
  }
}

async function addKnowledge(f: { title: string; content: string; category: string }) {
  await api.createKnowledge({ ...f, dataset_id: selectedDatasetId.value })
  knowledge.value = await api.knowledge(); dashboard.value = await api.dashboard(); config.value = await api.config()
}

async function deleteKnowledge(item: KnowledgeItem) {
  const confirmed = await showConfirm({
    title: '删除知识片段',
    message: `确定删除「${item.title}」吗？`,
    detail: '删除后会从业务知识库和向量索引中移除，相关问答将不再引用该片段。',
    confirmText: '确认删除',
  })
  if (!confirmed) return
  try { await api.deleteKnowledge(item.id); knowledge.value = knowledge.value.filter(e => e.id !== item.id); dashboard.value = await api.dashboard(); config.value = await api.config() }
  catch (err: any) { error.value = err.message || '删除知识片段失败' }
}

async function addAdmin(f: { username: string; password: string; role: string; dataset_ids: number[] }) {
  try { await api.createAdmin(f); admins.value = await api.admins() }
  catch (err: any) { error.value = err.message || '新增管理员失败' }
}

async function updateAdmin(id: number, f: { role: string; dataset_ids: number[] }) {
  try { await api.updateAdmin(id, f); admins.value = await api.admins() }
  catch (err: any) { error.value = err.message || '更新管理员失败' }
}

async function deleteAdmin(id: number) {
  try { await api.deleteAdmin(id); admins.value = admins.value.filter(a => a.id !== id) }
  catch (err: any) { error.value = err.message || '删除管理员失败' }
}

onMounted(bootstrap)
</script>

<template>
  <div v-if="!authChecked" class="auth-screen"><div class="auth-card"><div class="brand-mark"><AppIcon name="chart" :size="28" /></div><h1>DataAgent</h1><p>正在检查登录状态...</p></div></div>

  <div v-else-if="!currentAdmin" class="auth-screen">
    <form class="auth-card login-card" @submit.prevent="login">
      <div class="brand-mark"><AppIcon name="chart" :size="28" /></div><h1>数据智能体服务系统</h1>
      <p>请使用管理员账号登录后继续访问系统。</p>
      <label>账号<input v-model="loginForm.username" autocomplete="username" placeholder="请输入账号" /></label>
      <label>密码<input v-model="loginForm.password" autocomplete="current-password" placeholder="请输入密码" type="password" /></label>
      <button class="primary-btn" :disabled="loginLoading">{{ loginLoading ? '登录中...' : '登录' }}</button>
      <div v-if="error" class="auth-error">{{ error }}</div>
    </form>
  </div>

  <div v-else :class="['app-shell', { 'sidebar-collapsed': sidebarCollapsed }]">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark"><AppIcon name="chart" :size="24" /></div><div class="brand-copy"><strong>DataAgent</strong><small>企业数据智能体</small></div></div>
      <button class="sidebar-toggle" type="button" :title="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'" :aria-label="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'" @click="sidebarCollapsed = !sidebarCollapsed">
        <AppIcon :name="sidebarCollapsed ? 'expand' : 'collapse'" :size="17" />
      </button>
      <nav><button v-for="item in navItems" :key="item.id" :title="sidebarCollapsed ? item.label : ''" :class="['nav-item', { active: activeView === item.id }]" @click="activeView = item.id"><AppIcon :name="item.icon" /><span>{{ item.label }}</span></button></nav>
      <div class="sidebar-foot" :title="error ? '服务待连接' : '服务运行正常'"><div class="status-dot" :class="{ online: !error }" /><div><strong>{{ error ? '服务待连接' : '服务运行正常' }}</strong><small>{{ config?.llm_configured ? config.llm_model : '本地演示模式' }}</small></div></div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div><p>DATA INTELLIGENCE</p><h1>{{ pageTitle }}</h1></div>
        <div class="top-actions">
          <select v-model="selectedDatasetId" aria-label="选择数据集"><option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }}</option></select>
          <button class="account-chip" @click="activeView = 'accounts'">{{ currentAdmin?.username }}<span>{{ currentAdmin?.is_initial_admin ? '初始管理员' : '普通管理员' }}</span></button>
          <button class="logout-btn" @click="logout">退出</button>
        </div>
      </header>

      <div v-if="error" class="error-banner">{{ error }} <button @click="loadBase">重新连接</button></div>

      <OverviewView v-if="activeView === 'overview'" :dashboard="dashboard" :datasets="datasets" @analyze="analyze" @inspect="inspectDataset" @nav="(v: string) => activeView = v as ViewName" />
      <AnalystView v-else-if="activeView === 'analyst'" :sessions="sessions" :chat-messages="chatMessages" :result="result" :loading="loading" :session-id="sessionId" :examples="examples" :thinking-steps="thinkingSteps" :thinking-text="thinkingText" :thinking-collapsed="thinkingCollapsed" @analyze="analyze" @analyze-file="analyzeFile" @open-session="openSession" @delete-session="deleteSession" @new-session="newSession" @show-result="(m: ChatMessage) => { if (m.payload) result = m.payload }" @toggle-thinking="toggleThinking" />
      <DatasetsView v-else-if="activeView === 'datasets'" :datasets="datasets" :selected="selectedDataset" :selected-id="selectedDatasetId" :current-admin="currentAdmin" @upload="doUpload" @inspect="inspectDataset" @delete="deleteDataset" />
      <KnowledgeView v-else-if="activeView === 'knowledge'" :items="knowledge" @add="addKnowledge" @del="deleteKnowledge" />
      <AuditView v-else-if="activeView === 'audit'" />
      <AccountsView v-else-if="activeView === 'accounts'" :current="currentAdmin" :admins="admins" :datasets="datasets" @create="addAdmin" @update="updateAdmin" @delete="deleteAdmin" />
      <SettingsView v-else :config="config" @updated="(c: ConfigStatus) => config = c" />
    </main>
  </div>

  <Teleport to="body">
    <div v-if="confirmDialog.open" class="app-confirm-backdrop" @click.self="closeConfirm(false)">
      <section class="app-confirm-dialog" role="dialog" aria-modal="true" :aria-label="confirmDialog.title">
        <div class="app-confirm-icon" :class="confirmDialog.variant">
          <AppIcon name="warning" :size="28" />
        </div>
        <div class="app-confirm-body">
          <small>CONFIRM ACTION</small>
          <h3>{{ confirmDialog.title }}</h3>
          <p class="app-confirm-message">{{ confirmDialog.message }}</p>
          <p v-if="confirmDialog.detail" class="app-confirm-detail">{{ confirmDialog.detail }}</p>
        </div>
        <div class="app-confirm-actions">
          <button class="app-confirm-cancel" type="button" @click="closeConfirm(false)">{{ confirmDialog.cancelText }}</button>
          <button class="app-confirm-submit" type="button" @click="closeConfirm(true)">{{ confirmDialog.confirmText }}</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>
