<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ResultChart from '../components/ResultChart.vue'
import ThinkingBlock from '../components/ThinkingBlock.vue'
import TypewriterText from '../components/TypewriterText.vue'
import { api } from '../api'
import type { AnalysisResult, ChartType, ChatMessage, SessionSummary, ThinkingStep } from '../types'

const props = defineProps<{
  sessions: SessionSummary[]
  chatMessages: ChatMessage[]
  result: AnalysisResult | null
  loading: boolean
  sessionId: string | undefined
  examples: string[]
  thinkingSteps: ThinkingStep[]
  thinkingText: string
  thinkingCollapsed: boolean
}>()

const emit = defineEmits<{
  analyze: [q: string]
  analyzeFile: [file: File, q: string]
  openSession: [id: string]
  deleteSession: [id: string]
  newSession: []
  showResult: [msg: ChatMessage]
  updateSessions: []
  toggleThinking: []
}>()

const question = ref('')
const showSql = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const chosenFile = ref<File | null>(null)
const selectedChartSectionIndex = ref(0)
const chartSelections = ref<Record<string, ChartType>>({})
const resultRevision = ref(0)
const historyExpanded = ref(false)

function chooseFile(e: Event) {
  chosenFile.value = (e.target as HTMLInputElement).files?.[0] || null
  if (chosenFile.value) question.value = ''
}
function removeFile() {
  chosenFile.value = null
  if (fileInput.value) fileInput.value.value = ''
}

function submitInput() {
  if (props.loading) return
  if (chosenFile.value) {
    const file = chosenFile.value
    const q = question.value || ''
    question.value = ''
    removeFile()
    emit('analyzeFile', file, q)
    return
  }
  if (question.value) emit('analyze', question.value)
}

function isLatestMessage(index: number) {
  return index === props.chatMessages.length - 1
}

function isDocumentResult(result: AnalysisResult) {
  return result.execution_mode === 'document-llm' || result.execution_mode === 'local-document-parser'
}

function resultEyebrow(result: AnalysisResult) {
  if (isDocumentResult(result)) return 'DOCUMENT INSIGHT'
  return result.answer_type === 'knowledge_qa' ? 'KNOWLEDGE ANSWER' : 'ANALYSIS REPORT'
}

function insightTitle(result: AnalysisResult) {
  if (isDocumentResult(result)) return '文档分析结论'
  return result.answer_type === 'knowledge_qa' ? '知识库回答' : '关键发现'
}

function resultCountLabel(result: AnalysisResult) {
  if (isDocumentResult(result) && result.rows.length) return `${result.rows.length} 条可视化指标`
  return result.answer_type === 'knowledge_qa' ? `${result.knowledge_refs.length} 条知识依据` : `${result.rows.length} 条结果`
}

function tableTitle(result: AnalysisResult) {
  return isDocumentResult(result) ? '图表数据' : '查询结果'
}

type KnowledgeRef = AnalysisResult['knowledge_refs'][number]

function compactSourceTitle(title: string) {
  return String(title || '未命名依据')
    .replace(/[：:]\s*.*$/, '')
    .replace(/\.(pdf|docx?|md|txt|xlsx?|csv)$/i, '')
    .trim() || '未命名依据'
}

function sourceCategoryLabel(category?: string) {
  const value = String(category || '').toLowerCase()
  if (value.includes('upload') || value.includes('file')) return '上传文档'
  if (value.includes('business')) return '业务规则'
  if (value.includes('metric')) return '指标口径'
  if (value.includes('dictionary')) return '数据字典'
  return category || '知识片段'
}

function sourceBasisText(item: KnowledgeRef, index: number) {
  const parts = [`依据 ${index + 1}`, sourceCategoryLabel(item.category)]
  if (item.retrieval_mode) parts.push(item.retrieval_mode)
  if (typeof item.score === 'number') parts.push(`相关度 ${item.score.toFixed(3)}`)
  return parts.join(' · ')
}

function documentBasisLine(result: AnalysisResult) {
  const first = result.knowledge_refs?.[0]
  if (first?.title) return `分析依据：${compactSourceTitle(first.title)}。`
  return '分析依据：用户上传文档。'
}

type ChartSectionRef = NonNullable<AnalysisResult['chart_sections']>[number]

function chartSectionKey(section: ChartSectionRef | { id?: string; title?: string }, index: number) {
  return section.id || `section-${index}`
}

function chartTypeForSection(section: ChartSectionRef, index: number): ChartType {
  return chartSelections.value[chartSectionKey(section, index)] || section.chart?.type || 'bar'
}

function setChartTypeForSection(section: ChartSectionRef, index: number, type: ChartType) {
  chartSelections.value = {
    ...chartSelections.value,
    [chartSectionKey(section, index)]: type,
  }
}

function sectionKeywords(section: ChartSectionRef) {
  const chart = section.chart || {}
  const fields = [
    section.title,
    section.description,
    chart.title,
    chart.x_field,
    chart.y_field,
    chart.series_name,
    chart.series_field,
    ...(chart.series_fields || []),
    ...section.columns,
  ]
  const rowLabels = section.rows
    .flatMap((row) => section.columns.slice(0, 3).map((column) => row[column]))
    .map((value) => String(value ?? '').trim())
  return [...fields, ...rowLabels]
    .flatMap((value) => String(value || '').split(/[、，,。；;：:\s/()-]+/))
    .map((value) => value.trim())
    .filter((value, index, arr) => value.length >= 2 && value.length <= 28 && arr.indexOf(value) === index)
}

function insightsForSection(section: ChartSectionRef, result: AnalysisResult) {
  const own = (section.insights || []).map((item) => String(item || '').trim()).filter(Boolean)
  if (own.length) return own
  const keywords = sectionKeywords(section)
  if (!keywords.length) return []
  return (result.insights || [])
    .map((item) => String(item || '').trim())
    .filter((item) => item && keywords.some((keyword) => item.includes(keyword)))
    .slice(0, 3)
}

function topInsightLines(result: AnalysisResult) {
  if (isDocumentResult(result) && chartSections.value.length) return [documentBasisLine(result)]
  return result.insights || []
}

function assignedChartInsightSet(result: AnalysisResult) {
  const assigned = new Set<string>()
  chartSections.value.forEach((section) => {
    insightsForSection(section, result).forEach((item) => assigned.add(item))
  })
  return assigned
}

function overallInsightLines(result: AnalysisResult) {
  const insights = (result.insights || [])
    .map((item) => String(item || '').trim())
    .filter(Boolean)
  if (!chartSections.value.length) return insights
  const assigned = assignedChartInsightSet(result)
  const remaining = insights.filter((item) => !assigned.has(item) && item !== documentBasisLine(result))
  const preferred = remaining.filter((item) => /建议|关注|风险|应|需要|后续|优化|下钻|复盘|验证/.test(item))
  const selected = preferred.length ? preferred : remaining
  if (selected.length) return selected.slice(0, 6)
  if (isDocumentResult(result)) return ['建议结合上方各图表继续下钻异常指标、增长来源与风险项，并补充业务口径进行交叉验证。']
  if (chartSections.value.length) return ['建议围绕上方图表中的高值、低值、趋势拐点和异常波动继续下钻，结合业务规则验证原因。']
  return []
}

const chartSections = computed(() => {
  const current = props.result
  if (!current) return []
  const sections = (current.chart_sections || []).filter((section) =>
    section.chart?.type !== 'none' && section.rows?.length && section.columns?.length,
  )
  if (sections.length) return sections
  if (current.chart.type !== 'none' && current.rows.length) {
    return [{
      id: 'primary',
      title: current.chart.title,
      description: '当前分析结果的默认图表。',
      columns: current.columns,
      rows: current.rows,
      chart: current.chart,
      insights: [],
    }]
  }
  return []
})

const activeChartSection = computed(() => {
  const sections = chartSections.value
  if (!sections.length) return null
  const index = Math.min(selectedChartSectionIndex.value, sections.length - 1)
  return sections[index]
})

const activeChartResult = computed<AnalysisResult | null>(() => {
  if (!props.result) return null
  const section = activeChartSection.value
  if (!section) return props.result
  const index = Math.min(selectedChartSectionIndex.value, chartSections.value.length - 1)
  return chartResultForSection(section, index)
})

function chartResultForSection(section: ChartSectionRef, index: number): AnalysisResult {
  const current = props.result as AnalysisResult
  const selectedType = chartTypeForSection(section, index)
  return {
    ...current,
    columns: section.columns,
    rows: section.rows,
    chart: {
      ...section.chart,
      type: selectedType,
    },
  }
}

function scrollToChartSection(index: number) {
  selectedChartSectionIndex.value = index
  requestAnimationFrame(() => {
    document.getElementById(`chart-section-${index}`)?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  })
}

const reportChartOptions = computed(() => {
  if (!props.result) return undefined
  const sections = chartSections.value.map((section, index) => ({
    index,
    id: chartSectionKey(section, index),
    type: chartTypeForSection(section, index),
  }))
  if (!sections.length) return undefined
  return { sections }
})

watch(() => props.result, () => {
  resultRevision.value += 1
  selectedChartSectionIndex.value = 0
  showSql.value = false
  chartSelections.value = {}
}, { flush: 'sync' })

watch(() => chartSections.value.length, (length) => {
  if (selectedChartSectionIndex.value >= length) selectedChartSectionIndex.value = 0
})
</script>

<template>
  <section class="page analyst-page">
    <div class="analyst-layout">
      <div class="conversation-panel">
        <div class="analyst-welcome"><span><AppIcon name="spark" :size="25" /></span><div><h2>数据智能顾问</h2><p>支持数据分析、知识问答与连续追问</p></div><button class="new-chat-btn" @click="emit('newSession')">新对话</button></div>
        <div :class="['session-history-shell', { expanded: historyExpanded, collapsed: !historyExpanded }]">
          <button
            class="history-icon-toggle"
            type="button"
            :title="historyExpanded ? '收起历史对话' : '展开历史对话'"
            :aria-expanded="historyExpanded"
            @click="historyExpanded = !historyExpanded"
          >
            <AppIcon :name="historyExpanded ? 'collapse' : 'expand'" :size="18" />
          </button>
          <Transition name="history-slide">
            <div v-if="historyExpanded" class="session-history">
              <div class="session-history-title">
                <div>
                  <small>历史对话</small>
                  <strong>最近分析记录</strong>
                </div>
                <span>{{ sessions.length }} 个会话</span>
              </div>
              <div v-if="sessions.length" class="session-history-list">
                <div v-for="session in sessions" :key="session.id" :class="['session-history-item', { active: sessionId === session.id }]">
                  <button class="session-open-btn" type="button" @click="emit('openSession', session.id)">
                    <strong>{{ session.title }}</strong><small>{{ session.message_count }} 条消息 · {{ session.updated_at }}</small>
                  </button>
                  <button class="session-delete-btn" type="button" title="删除历史对话" @click="emit('deleteSession', session.id)">×</button>
                </div>
              </div>
              <div v-else class="session-history-empty">
                <AppIcon name="spark" :size="18" />
                <span>暂无历史对话</span>
              </div>
            </div>
          </Transition>
        </div>
        <div v-if="chatMessages.length" class="conversation-messages">
          <article v-for="(message, index) in chatMessages" :key="message.id || index" :class="['conversation-message', message.role]" @click="emit('showResult', message)">
            <small>{{ message.role === 'user' ? '你' : 'DataAgent' }}</small>
            <TypewriterText v-if="message._streamed" :text="message.content" :enabled="true" />
            <p v-else>{{ message.content }}</p>
            <em v-if="message.payload">查看该轮结果 →</em>
          </article>
        </div>
        <div v-else class="empty-chat"><AppIcon name="spark" :size="24"/><p>开始一次数据分析，或询问指标口径与业务规则。</p></div>
        <ThinkingBlock
          v-if="thinkingSteps.length"
          :steps="thinkingSteps"
          :thinking-text="thinkingText"
          :is-streaming="loading"
          :collapsed="thinkingCollapsed"
          @toggle="emit('toggleThinking')"
        />
        <div v-if="result?.context_applied" class="context-note"><AppIcon name="check" :size="15"/>已继承上一轮的分析条件</div>
        <div class="followups"><small>快捷提问</small><div><button v-for="item in examples" :key="item" @click="emit('analyze', item)">{{ item }}</button></div></div>
        <div :class="['chat-box', { 'has-file': chosenFile }]">
          <div v-if="chosenFile" class="file-chip">
            <AppIcon name="file" :size="14" />
            <span>{{ chosenFile.name }}</span>
            <button class="file-chip-remove" @click="removeFile" title="移除文件">&times;</button>
          </div>
          <div class="chat-input-row">
            <textarea v-model="question" rows="2" placeholder="输入问题，或上传 PDF/Word/Excel/CSV/PPT/MD/TXT 等文件进行分析..." @keydown.enter.exact.prevent="submitInput"/>
            <div class="chat-actions">
              <input ref="fileInput" type="file" accept=".pdf,.docx,.doc,.xlsx,.xls,.csv,.tsv,.pptx,.md,.markdown,.txt,.json,.jsonl,.html,.htm,.xml,.log" @change="chooseFile" hidden />
              <button class="icon-btn" title="上传文件分析" :disabled="loading" @click="fileInput?.click()"><AppIcon name="upload" :size="17" /></button>
              <button :disabled="loading || (!question && !chosenFile)" @click="submitInput"><AppIcon name="send" :size="18" /></button>
            </div>
          </div>
        </div>
      </div>
      <div class="result-panel">
        <div v-if="loading" class="analysis-loading-card">
          <div class="orbital-loader">
            <span></span>
            <i></i>
          </div>
          <div>
            <small>DATAAGENT REASONING</small>
            <h3>智能体正在解析与推理</h3>
            <p>正在读取文件、理解问题并组织分析结论，请稍等片刻。</p>
          </div>
        </div>
        <div v-if="!result" class="empty-result"><div><AppIcon name="chart" :size="34" /></div><h3>分析结果将在这里呈现</h3><p>选择一个示例问题，或在左侧输入你的业务问题。</p></div>
        <template v-else>
          <div class="report-header">
            <div>
              <small>{{ resultEyebrow(result) }}</small>
              <h2>{{ result.chart.title }}</h2>
            </div>
            <div class="export-actions" aria-label="导出报告">
              <a class="export-primary" :href="api.reportUrl(result.session_id, 'html', reportChartOptions)" target="_blank">
                <AppIcon name="eye" :size="16" />
                <span>预览报告</span>
              </a>
              <a :href="api.reportUrl(result.session_id, 'pdf', reportChartOptions)" target="_blank" title="导出 PDF">
                PDF
              </a>
              <a :href="api.reportUrl(result.session_id, 'docx', reportChartOptions)" target="_blank" title="导出 Word">
                Word
              </a>
              <a :href="api.reportUrl(result.session_id, 'md', reportChartOptions)" target="_blank" title="导出 Markdown">
                MD
              </a>
            </div>
          </div>
          <div class="result-meta"><span>{{ result.intent }}</span><span>{{ result.execution_mode }}</span><span>{{ resultCountLabel(result) }}</span><span v-if="chartSections.length > 1">{{ chartSections.length }} 组图表</span><span v-if="result.context_applied">已使用对话上下文</span></div>
          <div v-if="topInsightLines(result).length && isDocumentResult(result) && chartSections.length" class="insight-card document-basis-card">
            <h3><AppIcon name="book" :size="19" />文档依据</h3>
            <div v-for="(insight, index) in topInsightLines(result)" :key="insight">
              <span>{{ index + 1 }}</span>
              <p class="answer-text">{{ insight }}</p>
            </div>
          </div>
          <div v-if="chartSections.length" class="chart-stack">
            <div v-if="chartSections.length > 1" class="chart-section-switch chart-overview-switch">
              <div>
                <small>SMART CONTENTS</small>
                <strong>智能目录 · 本次生成 {{ chartSections.length }} 组图表</strong>
              </div>
              <div class="chart-section-tabs">
                <button
                  v-for="(section, index) in chartSections"
                  :key="section.id || index"
                  :class="{ active: selectedChartSectionIndex === index }"
                  :aria-current="selectedChartSectionIndex === index ? 'true' : undefined"
                  type="button"
                  @click="scrollToChartSection(index)"
                >
                  {{ section.title || `图表 ${index + 1}` }}
                </button>
              </div>
            </div>
            <article
              :id="`chart-section-${sectionIndex}`"
              v-for="(section, sectionIndex) in chartSections"
              :key="section.id || `chart-${sectionIndex}`"
              :class="['chart-card', 'multi-chart-card', { active: selectedChartSectionIndex === sectionIndex }]"
            >
              <div class="chart-card-heading">
                <div>
                  <small>CHART {{ sectionIndex + 1 }}</small>
                  <h3>{{ section.title || `图表 ${sectionIndex + 1}` }}</h3>
                  <p v-if="section.description">{{ section.description }}</p>
                </div>
              </div>
              <ResultChart
                :key="`${resultRevision}-${section.id || sectionIndex}`"
                :result="chartResultForSection(section, sectionIndex)"
                :model-value="chartTypeForSection(section, sectionIndex)"
                @update:model-value="setChartTypeForSection(section, sectionIndex, $event)"
              />
              <div v-if="insightsForSection(section, result).length" class="chart-linked-insights">
                <h4><AppIcon name="spark" :size="16" />该图提示</h4>
                <div v-for="(insight, index) in insightsForSection(section, result)" :key="`${section.id || sectionIndex}-${index}-${insight}`">
                  <span>{{ index + 1 }}</span>
                  <p>{{ insight }}</p>
                </div>
              </div>
            </article>
          </div>
          <div v-if="overallInsightLines(result).length" class="insight-card overall-insight-card"><h3><AppIcon name="spark" :size="19" />总体建议</h3><div v-for="(insight, index) in overallInsightLines(result)" :key="`${index}-${insight}`"><span>{{ index + 1 }}</span><p class="answer-text">{{ insight }}</p></div></div>
          <div v-else-if="topInsightLines(result).length && (!isDocumentResult(result) || !chartSections.length)" class="insight-card"><h3><AppIcon name="spark" :size="19" />{{ insightTitle(result) }}</h3><div v-for="(insight, index) in topInsightLines(result)" :key="insight"><span>{{ index + 1 }}</span><p class="answer-text">{{ insight }}</p></div></div>
          <div v-if="result.knowledge_refs.length" class="knowledge-sources">
            <h3><AppIcon name="book" :size="18"/>知识依据</h3>
            <div class="source-list">
              <article v-for="(item, index) in result.knowledge_refs" :key="`${item.id}-${index}`" class="source-item">
                <span class="source-index">{{ index + 1 }}</span>
                <div>
                  <strong>{{ compactSourceTitle(item.title) }}</strong>
                  <small>{{ sourceBasisText(item, index) }}</small>
                </div>
              </article>
            </div>
          </div>
          <div v-if="activeChartResult?.rows.length" class="data-table-card"><div class="subhead"><h3>{{ tableTitle(result) }}</h3><button v-if="result.sql" @click="showSql = !showSql">{{ showSql ? '隐藏 SQL' : '查看 SQL' }}</button></div>
            <pre v-if="showSql && result.sql" class="sql-block">{{ result.sql }}</pre>
            <div class="table-scroll"><table><thead><tr><th v-for="column in activeChartResult.columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in activeChartResult.rows" :key="index"><td v-for="column in activeChartResult.columns" :key="column">{{ row[column] }}</td></tr></tbody></table></div>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.analyst-page {
  background:
    radial-gradient(circle at 12% 8%, rgba(55, 128, 255, 0.12), transparent 26%),
    radial-gradient(circle at 88% 18%, rgba(18, 184, 190, 0.12), transparent 28%),
    linear-gradient(135deg, #f7fbff 0%, #eef7fb 48%, #f8fbff 100%);
}

.analyst-layout {
  backdrop-filter: blur(18px);
}

.conversation-panel {
  position: relative;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.92), rgba(247, 252, 255, 0.86)),
    radial-gradient(circle at 0 0, rgba(22, 119, 255, 0.08), transparent 36%);
  border-right: 1px solid rgba(198, 219, 233, 0.8);
}

.conversation-panel::before {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(64, 152, 203, 0.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(64, 152, 203, 0.04) 1px, transparent 1px);
  background-size: 28px 28px;
  mask-image: linear-gradient(180deg, #000 0%, transparent 88%);
}

.analyst-welcome,
.session-history-shell,
.session-history,
.chat-box,
.conversation-message,
.context-note {
  position: relative;
  z-index: 1;
}

.analyst-welcome {
  padding: 16px;
  border: 1px solid rgba(213, 230, 242, 0.92);
  border-radius: 22px;
  background: rgba(255, 255, 255, 0.76);
  box-shadow: 0 18px 45px rgba(41, 85, 122, 0.08);
}

.analyst-welcome > span {
  box-shadow: 0 12px 28px rgba(31, 130, 231, 0.28);
}

.new-chat-btn {
  border: 0;
  background: linear-gradient(135deg, #177cff, #19bdc7);
  color: white;
  box-shadow: 0 10px 22px rgba(24, 132, 219, 0.24);
}

.session-history-shell {
  min-height: 48px;
  display: flex;
  align-items: flex-start;
}

.session-history-shell.expanded {
  display: block;
}

.history-icon-toggle {
  width: 46px;
  height: 46px;
  border: 1px solid rgba(189, 216, 233, 0.82);
  border-radius: 16px;
  display: grid;
  place-items: center;
  color: #1f7fb6;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.9), rgba(231, 247, 255, 0.72)),
    radial-gradient(circle at 30% 20%, rgba(38, 148, 230, 0.2), transparent 42%);
  box-shadow: 0 14px 30px rgba(43, 96, 132, 0.12), inset 0 1px 0 rgba(255,255,255,.9);
  cursor: pointer;
  transition: transform .18s ease, box-shadow .18s ease, color .18s ease, background .18s ease;
}

.history-icon-toggle:hover {
  transform: translateY(-1px);
  color: #0b6bd3;
  box-shadow: 0 18px 38px rgba(39, 117, 180, 0.18), inset 0 1px 0 rgba(255,255,255,.95);
}

.session-history-shell.expanded .history-icon-toggle {
  position: absolute;
  top: 13px;
  right: 14px;
  width: 36px;
  height: 36px;
  border-radius: 13px;
  z-index: 3;
  color: #5f7f95;
  background: rgba(255, 255, 255, 0.82);
  box-shadow: 0 10px 24px rgba(52, 89, 118, 0.1);
}

.session-history {
  width: 100%;
  padding: 14px 58px 12px 14px;
  border-radius: 22px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.86), rgba(242, 250, 255, 0.78)),
    radial-gradient(circle at 10% 0, rgba(36, 143, 228, 0.11), transparent 42%);
  border: 1px solid rgba(199, 222, 237, 0.92);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.92), 0 16px 36px rgba(46, 91, 123, 0.08);
}

.session-history-title {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  color: #7f93a3;
}

.session-history-title div {
  min-width: 0;
}

.session-history-title small {
  display: block;
  color: #66a8bb;
  font-size: 9px;
  font-weight: 900;
  letter-spacing: .16em;
  text-transform: uppercase;
}

.session-history-title strong {
  display: block;
  margin-top: 3px;
  color: #203b54;
  font-size: 13px;
  letter-spacing: .01em;
}

.session-history-title span {
  flex: 0 0 auto;
  padding: 5px 9px;
  border-radius: 999px;
  color: #5f8498;
  background: rgba(232, 247, 252, 0.92);
  border: 1px solid rgba(204, 230, 238, 0.9);
  font-size: 9px;
  font-weight: 800;
}

.session-history-list {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding: 1px 0 7px;
  scrollbar-color: rgba(95, 132, 152, 0.45) transparent;
}

.session-history-item {
  min-width: 214px;
  max-width: 260px;
  min-height: 64px;
  background: rgba(255, 255, 255, 0.9);
  border-color: rgba(218, 232, 241, 0.95);
  box-shadow: 0 8px 22px rgba(48, 86, 114, 0.06);
}

.session-history-item.active {
  background:
    linear-gradient(135deg, rgba(236, 247, 255, 0.98), rgba(234, 253, 250, 0.96));
  border-color: rgba(96, 166, 235, 0.72);
  box-shadow: 0 13px 28px rgba(42, 136, 223, 0.15);
}

.session-history-list .session-open-btn {
  padding: 11px 12px;
}

.session-history-list .session-open-btn strong {
  color: #263f57;
  font-size: 12px;
  letter-spacing: .01em;
}

.session-history-list .session-open-btn small {
  margin-top: 6px;
  color: #8ca0af;
  font-size: 9px;
}

.session-history-list .session-delete-btn {
  color: #9aabb8;
  background: rgba(255,255,255,.42);
}

.session-history-list .session-delete-btn:hover {
  color: #d1435b;
  background: #fff1f2;
}

.session-history-empty {
  min-height: 62px;
  border: 1px dashed rgba(179, 211, 229, 0.9);
  border-radius: 16px;
  color: #7e98aa;
  display: grid;
  place-items: center;
  gap: 6px;
  background: rgba(255,255,255,.48);
  font-size: 10px;
  font-weight: 700;
}

.history-slide-enter-active,
.history-slide-leave-active {
  transition: opacity .18s ease, transform .18s ease, max-height .18s ease;
  overflow: hidden;
}

.history-slide-enter-from,
.history-slide-leave-to {
  opacity: 0;
  transform: translateY(-6px);
  max-height: 0;
}

.history-slide-enter-to,
.history-slide-leave-from {
  opacity: 1;
  transform: translateY(0);
  max-height: 160px;
}

.conversation-messages {
  position: relative;
  z-index: 1;
}

.conversation-message {
  border-radius: 18px;
  border-color: rgba(217, 231, 241, 0.96);
  box-shadow: 0 14px 35px rgba(31, 71, 105, 0.08);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
  font-family: Inter, Manrope, "HarmonyOS Sans SC", "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
}

.conversation-message:hover {
  transform: translateY(-1px);
  box-shadow: 0 18px 42px rgba(31, 71, 105, 0.12);
}

.conversation-message.user {
  background: linear-gradient(135deg, #eaf4ff, #edfaff);
}

.conversation-message.assistant {
  background: rgba(255, 255, 255, 0.92);
}

.conversation-message small {
  display: block;
  margin-bottom: 7px;
  color: #7e96a9;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .02em;
}

.conversation-message p,
.conversation-message :deep(.typewriter-text) {
  display: block;
  color: #243b53;
  font-size: 12px;
  line-height: 1.78;
  font-weight: 520;
  letter-spacing: .01em;
  white-space: pre-line;
}

.conversation-message.assistant :deep(.typewriter-text) {
  color: #21384f;
}

.conversation-message.user p {
  color: #2d4962;
  font-size: 11px;
}

.conversation-message em {
  margin-top: 9px;
  padding-top: 8px;
  border-top: 1px solid rgba(225, 235, 242, 0.8);
  color: #1685b7;
  font-size: 9px;
  font-weight: 800;
}

.result-panel {
  position: relative;
  background:
    radial-gradient(circle at 18% 8%, rgba(74, 149, 247, 0.12), transparent 28%),
    radial-gradient(circle at 90% 0%, rgba(18, 184, 190, 0.13), transparent 25%),
    linear-gradient(180deg, #f4f9fc 0%, #eef5f9 100%);
}

.result-panel::before {
  content: "";
  position: fixed;
  inset: 84px 0 0 38%;
  pointer-events: none;
  background:
    linear-gradient(120deg, transparent 0%, rgba(255,255,255,.32) 34%, transparent 62%);
  opacity: .42;
}

.report-header,
.result-meta,
.chart-card,
.insight-card,
.knowledge-sources,
.data-table-card,
.empty-result,
.analysis-loading-card {
  position: relative;
  z-index: 1;
}

.report-header {
  padding: 18px 20px;
  border: 1px solid rgba(211, 227, 239, 0.95);
  border-radius: 22px;
  background: rgba(255, 255, 255, 0.82);
  box-shadow: 0 18px 44px rgba(42, 80, 110, 0.08);
}

.export-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px;
  border: 1px solid rgba(207, 227, 239, 0.92);
  border-radius: 18px;
  background: linear-gradient(135deg, rgba(247, 252, 255, 0.92), rgba(235, 248, 251, 0.92));
  box-shadow: inset 0 1px 0 rgba(255,255,255,.92), 0 12px 28px rgba(44, 85, 115, 0.08);
}

.export-actions a {
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 34px;
  padding: 0 12px;
  border-radius: 12px;
  color: #47708a;
  background: rgba(255, 255, 255, 0.78);
  border: 1px solid rgba(215, 230, 239, 0.95);
  font-size: 10px;
  font-weight: 800;
  transition: transform .16s ease, box-shadow .16s ease, color .16s ease, background .16s ease;
}

.export-actions a:hover {
  transform: translateY(-1px);
  color: #0e79b9;
  box-shadow: 0 10px 22px rgba(35, 103, 153, 0.14);
}

.export-actions .export-primary {
  gap: 8px;
  color: white;
  background: linear-gradient(135deg, #177cff, #19b9c6);
  border: 0;
  padding: 0 15px;
  box-shadow: 0 12px 24px rgba(24, 132, 219, 0.25);
}

.export-actions .export-primary:hover {
  color: white;
  box-shadow: 0 16px 30px rgba(24, 132, 219, 0.32);
}

.chart-card,
.insight-card,
.knowledge-sources,
.data-table-card {
  border-radius: 24px;
  border-color: rgba(211, 227, 239, 0.9);
  background: rgba(255, 255, 255, 0.86);
  box-shadow: 0 22px 52px rgba(45, 82, 112, 0.08);
  backdrop-filter: blur(14px);
}

.chart-stack {
  display: grid;
  gap: 18px;
}

.chart-overview-switch {
  position: sticky;
  top: 14px;
  z-index: 4;
  box-shadow: 0 16px 34px rgba(38, 78, 110, 0.08);
}

.multi-chart-card {
  display: grid;
  gap: 14px;
  scroll-margin-top: 118px;
}

.multi-chart-card.active {
  border-color: rgba(73, 152, 255, 0.42);
  box-shadow: 0 22px 52px rgba(32, 108, 186, 0.12);
}

.chart-card-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 18px;
  padding: 2px 4px 0;
}

.chart-card-heading small {
  display: block;
  margin-bottom: 4px;
  color: #62a8bb;
  font-size: 9px;
  font-weight: 900;
  letter-spacing: .18em;
}

.chart-card-heading h3 {
  margin: 0;
  color: #173a55;
  font-size: 16px;
  font-weight: 900;
}

.chart-card-heading p {
  margin: 5px 0 0;
  color: #60778a;
  font-size: 11px;
  line-height: 1.7;
}

.chart-section-switch {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px;
  border-radius: 18px;
  background:
    linear-gradient(135deg, rgba(235, 246, 255, 0.9), rgba(231, 252, 248, 0.82));
  border: 1px solid rgba(204, 225, 238, 0.78);
}

.chart-section-switch small {
  display: block;
  color: #65a7bb;
  font-size: 9px;
  font-weight: 900;
  letter-spacing: .16em;
}

.chart-section-switch strong {
  display: block;
  color: #173a55;
  font-size: 13px;
  margin-top: 3px;
}

.chart-section-tabs {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.chart-section-tabs button {
  border: 1px solid rgba(202, 222, 235, 0.94);
  border-radius: 999px;
  padding: 8px 12px;
  color: #536e82;
  background: rgba(255, 255, 255, 0.82);
  font-size: 10px;
  font-weight: 800;
  cursor: pointer;
  transition: transform .16s ease, box-shadow .16s ease, color .16s ease, background .16s ease;
}

.chart-section-tabs button:hover {
  transform: translateY(-1px);
  color: #1576d9;
  box-shadow: 0 10px 22px rgba(39, 104, 150, 0.12);
}

.chart-section-tabs button.active {
  color: white;
  border-color: transparent;
  background: linear-gradient(135deg, #177cff, #19b9c6);
  box-shadow: 0 12px 24px rgba(24, 132, 219, 0.24);
}

.chart-section-note {
  color: #60778a;
  font-size: 11px;
  line-height: 1.7;
  padding: 0 4px;
}

.chart-linked-insights {
  display: grid;
  gap: 9px;
  margin-top: 4px;
  padding: 14px;
  border: 1px solid rgba(216, 231, 241, 0.88);
  border-radius: 18px;
  background:
    linear-gradient(135deg, rgba(248, 252, 255, 0.96), rgba(238, 251, 249, 0.88));
}

.chart-linked-insights h4 {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 2px;
  color: #146f82;
  font-size: 13px;
}

.chart-linked-insights > div {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding-top: 9px;
  border-top: 1px solid rgba(225, 236, 243, 0.82);
}

.chart-linked-insights span {
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  color: #0f8d84;
  background: linear-gradient(135deg, #e4faf4, #eaf4ff);
  font-size: 9px;
  font-weight: 900;
}

.chart-linked-insights p {
  margin: 1px 0 0;
  color: #2d465d;
  font-size: 11px;
  line-height: 1.72;
}

.insight-card > div {
  border-top-color: rgba(226, 236, 243, 0.9);
}

.insight-card > div span {
  background: linear-gradient(135deg, #e7fbf5, #eaf4ff);
  box-shadow: inset 0 0 0 1px rgba(28, 174, 154, 0.1);
}

.knowledge-sources {
  padding: 20px 22px;
}

.knowledge-sources h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 13px;
  color: #166d82;
  font-size: 14px;
}

.source-list {
  display: grid;
  gap: 10px;
}

.source-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid rgba(218, 232, 241, 0.9);
  border-radius: 16px;
  background:
    linear-gradient(135deg, rgba(247, 252, 255, 0.95), rgba(239, 251, 249, 0.9));
}

.source-index {
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #0f8d84;
  background: linear-gradient(135deg, #e4faf4, #eaf4ff);
  font-size: 11px;
  font-weight: 900;
}

.source-item div {
  min-width: 0;
}

.source-item strong {
  display: block;
  color: #1a3852;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-item small {
  display: block;
  margin-top: 4px;
  color: #7890a2;
  font-size: 9px;
  font-weight: 700;
}

.empty-result {
  min-height: calc(100vh - 170px);
  border: 1px dashed rgba(164, 198, 220, 0.72);
  border-radius: 28px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.76), rgba(247,252,255,.82)),
    radial-gradient(circle at center, rgba(32, 155, 205, 0.08), transparent 32%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.9), 0 22px 50px rgba(50, 85, 112, 0.06);
}

.empty-result div {
  background: linear-gradient(135deg, #e7f4ff, #dcfbf7);
  box-shadow: 0 16px 35px rgba(47, 142, 206, 0.16);
}

.analysis-loading-card {
  display: flex;
  align-items: center;
  gap: 18px;
  margin-bottom: 18px;
  padding: 18px 20px;
  border: 1px solid rgba(186, 220, 241, 0.92);
  border-radius: 24px;
  background:
    linear-gradient(135deg, rgba(255,255,255,.92), rgba(235,249,253,.9)),
    radial-gradient(circle at 12% 50%, rgba(21, 125, 255, .12), transparent 34%);
  box-shadow: 0 24px 60px rgba(29, 91, 136, 0.13);
}

.analysis-loading-card small {
  color: #26a3b6;
  font-size: 9px;
  letter-spacing: 1.8px;
  font-weight: 900;
}

.analysis-loading-card h3 {
  margin: 5px 0 5px;
  color: #173853;
  font-size: 17px;
}

.analysis-loading-card p {
  margin: 0;
  color: #688195;
  font-size: 11px;
}

.orbital-loader {
  width: 62px;
  height: 62px;
  border-radius: 50%;
  position: relative;
  display: grid;
  place-items: center;
  background: conic-gradient(from 0deg, #1979ff, #17c4c7, #dff9ff, #1979ff);
  animation: spin-orbit 1.05s linear infinite;
  box-shadow: 0 13px 30px rgba(24, 132, 219, 0.28);
}

.orbital-loader::before {
  content: "";
  position: absolute;
  inset: 7px;
  border-radius: inherit;
  background: #f8fdff;
}

.orbital-loader span {
  position: relative;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: linear-gradient(135deg, #177cff, #14bdc4);
  box-shadow: 0 0 18px rgba(20, 171, 219, 0.45);
}

.orbital-loader i {
  position: absolute;
  top: 4px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: white;
  box-shadow: 0 0 16px rgba(255,255,255,.9);
}

@keyframes spin-orbit {
  to { transform: rotate(360deg); }
}

:deep(.thinking-block) {
  border: 1px solid rgba(186, 220, 241, 0.95);
  border-radius: 20px;
  background: rgba(255,255,255,.82);
  box-shadow: 0 18px 38px rgba(36, 82, 116, 0.09);
  backdrop-filter: blur(12px);
}

:deep(.thinking-block.streaming) {
  background:
    linear-gradient(135deg, rgba(255,255,255,.94), rgba(235,249,253,.92)),
    radial-gradient(circle at 8% 20%, rgba(24,132,219,.13), transparent 32%);
}

:deep(.thinking-ring) {
  position: relative;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: conic-gradient(#1a7cff, #18c0c8, #dcefff, #1a7cff);
  box-shadow: 0 8px 20px rgba(28, 135, 219, .22);
}

:deep(.thinking-ring::before) {
  content: "";
  position: absolute;
  inset: 5px;
  border-radius: inherit;
  background: white;
}

:deep(.thinking-ring span) {
  position: relative;
  color: #1674d4;
  font-size: 10px;
  font-weight: 900;
}

:deep(.thinking-ring.spinning) {
  animation: spin-orbit .95s linear infinite;
}

:deep(.thinking-ring.spinning span) {
  animation: counter-spin .95s linear infinite;
}

@keyframes counter-spin {
  to { transform: rotate(-360deg); }
}

:deep(.thinking-block-header .arrow) {
  width: auto;
  padding: 4px 8px;
  border-radius: 999px;
  background: #eef7fc;
  color: #5d7f94;
  font-size: 9px;
}

:deep(.thinking-step) {
  border-radius: 12px;
}

.chat-box {
  display: grid;
  grid-template-columns: 1fr;
  align-items: stretch;
  gap: 8px;
  padding: 10px;
  border-radius: 24px;
  border: 1px solid rgba(186, 213, 231, 0.96);
  background: rgba(255, 255, 255, 0.88);
  box-shadow: 0 24px 55px rgba(34, 78, 112, 0.12);
  backdrop-filter: blur(16px);
}

.chat-input-row {
  min-width: 0;
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.chat-input-row textarea {
  width: 100%;
  min-width: 0;
  min-height: 48px;
  padding: 12px 8px 10px 12px;
  color: #25445e;
}

.chat-actions {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.file-chip {
  width: fit-content;
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px 7px 10px;
  border: 1px solid #dbe9f2;
  border-radius: 16px;
  background: linear-gradient(135deg, #f4f9fd, #eefbfb);
  color: #244258;
  font-size: 12px;
  box-shadow: 0 10px 22px rgba(38, 77, 108, 0.08);
}

.file-chip span {
  max-width: min(360px, calc(100vw - 420px));
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.chat-box .file-chip-remove {
  width: 24px;
  height: 24px;
  border-radius: 8px;
  background: #e9f2fb;
  color: #527089;
  box-shadow: none;
  font-size: 17px;
  line-height: 1;
}

.chat-box .file-chip-remove:hover {
  color: #c24154;
  background: #fff1f3;
}

.chat-box.has-file {
  padding-top: 12px;
}
</style>
