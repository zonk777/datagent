<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { AnalysisResult, ChartType as ResultChartType } from '../types'

type ChartType = ResultChartType

const props = defineProps<{ result: AnalysisResult; modelValue?: ChartType }>()
const emit = defineEmits<{ 'update:modelValue': [value: ChartType] }>()
const target = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

const palette = ['#1677ff', '#13b8c8', '#7c4dff', '#f59e0b', '#10b981', '#ef5da8']
const chartTypeOptions: Array<{ value: ChartType; label: string }> = [
  { value: 'bar', label: '柱状图' },
  { value: 'line', label: '折线图' },
  { value: 'pie', label: '饼图' },
  { value: 'scatter', label: '散点图' },
  { value: 'area', label: '面积图' },
  { value: 'radar', label: '雷达图' },
  { value: 'none', label: '不生成图像' },
]

const selectedType = ref<ChartType>((props.modelValue || props.result.chart.type || 'bar') as ChartType)

function chartName(type: string | null | undefined) {
  return chartTypeOptions.find((item) => item.value === type)?.label || '图表'
}

function numberValue(value: unknown) {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

function baseFields() {
  const spec = props.result.chart
  const x = spec.x_field || props.result.columns[0]
  const y = spec.y_field || props.result.columns[props.result.columns.length - 1]
  const secondaryY = spec.secondary_y_field && props.result.rows.some((row) => row[spec.secondary_y_field!] !== undefined)
    ? spec.secondary_y_field
    : null
  const labels = [...new Set(props.result.rows.map((row) => String(row[x] ?? '')))]
  const seriesFields = spec.series_fields?.length
    ? spec.series_fields
    : (spec.series_field ? [spec.series_field] : [])
  const seriesLabel = (row: Record<string, unknown>) =>
    seriesFields.map((field) => String(row[field] ?? '未分类')).join(' / ')
  const seriesNames = seriesFields.length
    ? [...new Set(props.result.rows.map((row) => seriesLabel(row)))]
    : [spec.series_name || y]
  return { spec, x, y, secondaryY, labels, seriesFields, seriesLabel, seriesNames }
}

function emptyOption(title = '暂无可视化数据'): echarts.EChartsOption {
  return {
    title: { text: title, left: 'center', top: 'middle', textStyle: { color: '#8aa0b3', fontSize: 14 } },
  }
}

function dataFor(label: string, field: string, seriesFields: string[], seriesLabel: (row: Record<string, unknown>) => string, seriesName?: string) {
  const row = props.result.rows.find((item) =>
    String(item[labelField.value] ?? '') === label && (!seriesFields.length || seriesLabel(item) === seriesName),
  )
  return row ? numberValue(row[field]) : null
}

const labelField = computed(() => baseFields().x)
const recommendation = computed(() => props.result.chart.recommendation)
const displayModeNote = computed(() => {
  const mode = props.result.chart.display_mode || recommendation.value?.display_mode
  if (mode === 'dual_axis' && props.result.chart.secondary_y_field) {
    return `建议双轴：左轴 ${props.result.chart.y_field}，右轴 ${props.result.chart.secondary_y_field}`
  }
  if (mode === 'facet') {
    const fields = props.result.chart.facet_fields?.length ? props.result.chart.facet_fields.join(' / ') : '系列维度'
    return `建议分面：当前结果系列较多，可按 ${fields} 拆分查看`
  }
  return ''
})

const option = computed<echarts.EChartsOption>(() => {
  if (!props.result.rows.length || props.result.chart.type === 'none' || selectedType.value === 'none') {
    return emptyOption(selectedType.value === 'none' ? '已选择不生成图像' : undefined)
  }

  const { spec, x, y, secondaryY, labels, seriesFields, seriesLabel, seriesNames } = baseFields()
  const chartType = selectedType.value
  if (!x || !y || !labels.length) return emptyOption()

  if (chartType === 'pie') {
    const pieData = props.result.rows.map((row) => ({
      name: seriesFields.length ? `${String(row[x] ?? '')} / ${seriesLabel(row)}` : String(row[x] ?? ''),
      value: numberValue(row[y]),
    }))
    return {
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll', bottom: 0 },
      series: [{
        type: 'pie',
        radius: ['42%', '70%'],
        data: pieData,
        itemStyle: { borderRadius: 7, borderWidth: 3, borderColor: '#fff' },
      }],
      color: palette,
    }
  }

  if (chartType === 'radar') {
    const radarLabels = labels.slice(0, 12)
    const radarSeriesNames = !seriesFields.length && secondaryY ? [y, secondaryY] : seriesNames
    const valueFor = (label: string, name: string) => {
      if (!seriesFields.length && (name === y || name === secondaryY)) {
        const row = props.result.rows.find((item) => String(item[x] ?? '') === label)
        return row ? numberValue(row[name]) : 0
      }
      const row = props.result.rows.find((item) =>
        String(item[x] ?? '') === label && (!seriesFields.length || seriesLabel(item) === name),
      )
      return row ? numberValue(row[y]) : 0
    }
    const allValues = radarLabels.flatMap((label) => radarSeriesNames.map((name) => valueFor(label, name)))
    const maxValue = Math.max(1, ...allValues)
    return {
      color: palette,
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll', bottom: 0 },
      radar: {
        radius: '62%',
        indicator: radarLabels.map((label) => ({ name: label, max: maxValue * 1.15 })),
        splitLine: { lineStyle: { color: '#e6eef5' } },
        splitArea: { areaStyle: { color: ['#fbfdff', '#f3f8fc'] } },
        axisName: { color: '#657b8d', fontSize: 11 },
      },
      series: [{
        type: 'radar',
        data: radarSeriesNames.map((name) => ({
          name,
          value: radarLabels.map((label) => valueFor(label, name)),
          areaStyle: { opacity: 0.12 },
          symbolSize: 5,
        })),
      }],
    }
  }

  const buildSeries = (name: string, field: string, seriesIndex: number, yAxisIndex = 0): echarts.SeriesOption => {
    const data = labels.map((label) => dataFor(label, field, seriesFields, seriesLabel, name))
    const color = palette[seriesIndex % palette.length]

    if (chartType === 'line' || chartType === 'area') {
      return {
        name,
        type: 'line',
        yAxisIndex,
        smooth: true,
        connectNulls: false,
        data,
        symbolSize: 6,
        lineStyle: { width: 2.5, color },
        itemStyle: { color },
        areaStyle: chartType === 'area' ? { opacity: 0.16, color } : undefined,
      }
    }

    if (chartType === 'scatter') {
      return {
        name,
        type: 'scatter',
        yAxisIndex,
        data,
        symbolSize: (value: unknown) => Math.max(7, Math.min(22, Math.sqrt(numberValue(value)) / 5)),
        itemStyle: { color, opacity: 0.82 },
      }
    }

    return {
      name,
      type: 'bar',
      yAxisIndex,
      data,
      itemStyle: { color, borderRadius: [5, 5, 0, 0] },
    }
  }

  const series: echarts.SeriesOption[] = !seriesFields.length && secondaryY
    ? [buildSeries(y, y, 0, 0), buildSeries(secondaryY, secondaryY, 1, 1)]
    : seriesNames.map((name, seriesIndex) => buildSeries(name, y, seriesIndex))

  return {
    color: palette,
    dataZoom: labels.length > 16 ? [{ type: 'slider', height: 18, bottom: 16 }, { type: 'inside' }] : undefined,
    grid: {
      left: 62,
      right: secondaryY ? 72 : 28,
      top: seriesFields.length || secondaryY ? 72 : 28,
      bottom: labels.length > 16 ? 78 : (labels.length > 20 ? 62 : 46),
    },
    tooltip: { trigger: 'axis' },
    legend: seriesFields.length || secondaryY ? { type: 'scroll', top: 2, left: 8, right: 8, textStyle: { color: '#5d7386', fontSize: 11 } } : undefined,
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: '#d7e2ed' } },
      axisLabel: { color: '#718096', rotate: labels.length > 20 ? 35 : 0 },
    },
    yAxis: secondaryY
      ? [
          { type: 'value', name: y, splitLine: { lineStyle: { color: '#eef3f7' } }, axisLabel: { color: '#718096' } },
          { type: 'value', name: secondaryY, splitLine: { show: false }, axisLabel: { color: '#718096' } },
        ]
      : { type: 'value', splitLine: { lineStyle: { color: '#eef3f7' } }, axisLabel: { color: '#718096' } },
    series,
  }
})

function render() {
  if (!target.value) return
  chart ||= echarts.init(target.value)
  chart.setOption(option.value, true)
  chart.resize()
}

function resize() { chart?.resize() }

onMounted(() => { render(); window.addEventListener('resize', resize) })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose() })
watch(() => props.result.chart.type, (type) => {
  if (props.modelValue === undefined) selectedType.value = (type || 'bar') as ChartType
})
watch(() => props.modelValue, (type) => {
  if (type) selectedType.value = type
})
watch(selectedType, (type) => emit('update:modelValue', type))
watch(option, () => nextTick(render), { deep: true })
</script>

<template>
  <div class="chart-widget">
    <div class="chart-toolbar">
      <div>
        <strong>推荐：{{ chartName(result.chart.type) }}</strong>
        <small v-if="recommendation">{{ recommendation.reason }}</small>
        <small v-if="displayModeNote">{{ displayModeNote }}</small>
      </div>
      <label>
        手动切换
        <select v-model="selectedType">
          <option v-for="item in chartTypeOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
    </div>
    <div ref="target" class="result-chart" role="img" :aria-label="result.chart.title" />
  </div>
</template>

<style scoped>
.chart-widget { display: grid; gap: 12px; }
.chart-toolbar { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; }
.chart-toolbar strong { display: block; color: #173a55; font-size: 12px; }
.chart-toolbar small { display: block; color: #6d8192; font-size: 10px; line-height: 1.6; margin-top: 3px; }
.chart-toolbar label { display: flex; align-items: center; gap: 8px; color: #63798c; font-size: 10px; white-space: nowrap; }
.chart-toolbar select { border: 1px solid #d8e5ee; border-radius: 9px; padding: 7px 9px; color: #355068; background: #fff; font-size: 10px; outline: 0; }
</style>
