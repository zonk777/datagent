<script setup lang="ts">
import { computed, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import type { AdminUser, Dataset } from '../types'

const props = defineProps<{
  datasets: Dataset[]
  selected: Dataset | null
  selectedId: number | undefined
  currentAdmin?: AdminUser | null
}>()
const emit = defineEmits<{
  upload: [file: File, name: string, desc: string]
  inspect: [id: number]
  delete: [id: number]
}>()

const file = ref<File | null>(null)
const name = ref('')
const desc = ref('')
const uploading = ref(false)
const progress = ref(0)
const status = ref('')
const datasetQuery = ref('')
const canManageDatasets = computed(() => {
  const role = props.currentAdmin?.role
  return !!props.currentAdmin?.is_initial_admin || role === 'initial_admin' || role === 'admin' || role === 'data_analyst'
})

const filteredDatasets = computed(() => {
  const q = datasetQuery.value.trim().toLowerCase()
  if (!q) return props.datasets
  return props.datasets.filter(ds => {
    const text = [
      ds.name,
      ds.description,
      ds.table_name,
      ds.source_type,
      ds.status,
      datasetAccessLabel(ds),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
    return text.includes(q)
  })
})

function hasDatasetAccess(ds: Dataset) {
  const admin = props.currentAdmin
  if (!admin) return false
  if (admin.is_initial_admin) return true
  const permissions = admin.dataset_permissions || []
  if ((admin.role === 'admin' || admin.role === 'data_analyst') && permissions.length === 0) return true
  return permissions.includes(ds.id)
}

function datasetAccessLabel(ds: Dataset) {
  return hasDatasetAccess(ds) ? '有权限' : '无权限'
}

function canDeleteDataset(ds: Dataset) {
  return canManageDatasets.value && hasDatasetAccess(ds)
}

function choose(e: Event) {
  file.value = (e.target as HTMLInputElement).files?.[0] || null
  if (file.value && !name.value) name.value = file.value.name.replace(/\.[^.]+$/, '')
  progress.value = 0
  status.value = ''
}

async function doUpload() {
  if (!file.value) return
  uploading.value = true
  progress.value = 0
  status.value = '准备上传'
  await emit('upload', file.value, name.value, desc.value)
  uploading.value = false
  status.value = '导入完成'
}
</script>

<template>
  <section class="page">
    <div class="page-intro">
      <div>
        <span class="eyebrow"><AppIcon name="database" :size="16" /> DATA CATALOG</span>
        <h2>数据源与元数据</h2>
        <p>上传 CSV / Excel / MySQL 导入，系统会自动识别字段、样例和缺失情况。</p>
      </div>
    </div>

    <div class="dataset-layout">
      <article class="panel upload-panel">
        <h3><AppIcon name="upload" />导入数据文件</h3>
        <label class="drop-zone">
          <input type="file" accept=".csv,.xls,.xlsx" @change="choose"/>
          <AppIcon name="upload" :size="32"/>
          <strong>{{ file?.name || '选择 CSV / Excel 文件' }}</strong>
          <small>单文件最大 100MB</small>
        </label>
        <input v-model="name" placeholder="数据集名称（可选）"/>
        <textarea v-model="desc" placeholder="数据集用途与说明" rows="3"/>
        <div v-if="uploading || progress > 0" class="upload-progress">
          <div><span>{{ status || '等待上传' }}</span><b>{{ progress }}%</b></div>
          <i><span :style="{ width: `${progress}%` }"/></i>
        </div>
        <button class="primary-btn" :disabled="!file || uploading" @click="doUpload">
          {{ uploading ? '正在导入...' : '开始导入' }}
        </button>
      </article>

      <article class="panel dataset-panel">
        <div class="panel-title dataset-panel-title">
          <div>
            <small>CONNECTED</small>
            <h3>已接入数据集</h3>
          </div>
          <div class="dataset-search">
            <AppIcon name="search" :size="15" />
            <input v-model="datasetQuery" placeholder="搜索数据源名称、表名、类型或权限" />
            <button v-if="datasetQuery" type="button" aria-label="清空搜索" @click="datasetQuery = ''">×</button>
          </div>
        </div>

        <div v-if="filteredDatasets.length" class="dataset-cards">
          <article
            v-for="ds in filteredDatasets"
            :key="ds.id"
            class="dataset-card"
          >
            <button
              class="dataset-card-main"
              :class="{ selected: selectedId === ds.id }"
              type="button"
              @click="emit('inspect', ds.id)"
            >
              <span><AppIcon name="database" /></span>
              <div>
                <strong>{{ ds.name }}</strong>
                <small>{{ ds.description || '暂无描述' }}</small>
                <em>{{ ds.row_count.toLocaleString() }} 行 · {{ ds.column_count }} 字段</em>
              </div>
              <div class="dataset-card-tags" aria-label="数据源状态">
                <i>{{ ds.source_type }}</i>
                <b :class="hasDatasetAccess(ds) ? 'access-ok' : 'access-denied'">
                  {{ datasetAccessLabel(ds) }}
                </b>
              </div>
            </button>
            <button
              v-if="canDeleteDataset(ds)"
              class="dataset-delete-btn"
              type="button"
              title="删除数据集"
              aria-label="删除数据集"
              @click.stop="emit('delete', ds.id)"
            >
              ×
            </button>
          </article>
        </div>
        <div v-else class="dataset-empty">
          <AppIcon name="search" :size="28" />
          <strong>没有找到匹配的数据源</strong>
          <p>换个关键词，或者清空搜索条件后再试。</p>
        </div>
      </article>
    </div>

    <article v-if="selected" class="panel metadata-panel">
      <div class="panel-title">
        <div>
          <small>METADATA</small>
          <h3>{{ selected.name }} · 字段说明</h3>
        </div>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>字段</th><th>类型</th><th>业务说明</th><th>样例值</th><th>缺失率</th></tr>
          </thead>
          <tbody>
            <tr v-for="col in selected.columns" :key="col.name">
              <td><code>{{ col.name }}</code></td>
              <td>{{ col.data_type }}</td>
              <td>{{ col.description }}</td>
              <td>{{ col.sample_value }}</td>
              <td>{{ (col.null_rate * 100).toFixed(1) }}%</td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
  </section>
</template>

<style scoped>
.dataset-panel-title {
  gap: 14px;
}

.dataset-search {
  width: min(330px, 100%);
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid #dce8f0;
  border-radius: 11px;
  padding: 8px 10px;
  color: #7d93a5;
  background: #fff;
}

.dataset-search input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  color: #2f4a61;
  font-size: 11px;
  background: transparent;
}

.dataset-search button {
  width: 20px;
  height: 20px;
  border: 0;
  border-radius: 50%;
  background: #f1f5f9;
  color: #7b8fa0;
  line-height: 1;
}

.dataset-card {
  position: relative;
}

.dataset-card-main {
  width: 100%;
  height: 100%;
  padding-right: 46px !important;
}

.dataset-delete-btn {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 26px !important;
  height: 26px;
  min-width: 26px !important;
  max-width: 26px !important;
  display: grid !important;
  place-items: center;
  padding: 0 !important;
  border: 1px solid #ffd8df !important;
  border-radius: 9px !important;
  color: #c53c55 !important;
  background: #fff5f6 !important;
  font-size: 17px !important;
  font-weight: 700;
  line-height: 1;
  box-shadow: none !important;
  z-index: 2;
}

.dataset-delete-btn:hover {
  color: #a91f3d !important;
  background: #ffe9ed !important;
  transform: translateY(-1px);
}

.dataset-card-tags {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 7px;
  margin-left: auto;
}

.dataset-cards .dataset-card-tags i,
.dataset-cards .dataset-card-tags b {
  width: max-content;
  font-style: normal;
  font-size: 8px;
  font-weight: 800;
  border-radius: 999px;
  padding: 4px 7px;
  line-height: 1;
}

.dataset-cards .dataset-card-tags i {
  color: #728294;
  background: #f0f4f8;
  text-transform: uppercase;
}

.dataset-cards .dataset-card-tags b.access-ok {
  color: #078b65;
  background: #e6f8ef;
}

.dataset-cards .dataset-card-tags b.access-denied {
  color: #c24154;
  background: #fff1f3;
}

.dataset-empty {
  min-height: 190px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 8px;
  color: #9aabb9;
  text-align: center;
}

.dataset-empty strong {
  color: #415a70;
  font-size: 13px;
}

.dataset-empty p {
  margin: 0;
  font-size: 10px;
}
</style>
