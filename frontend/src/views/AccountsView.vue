<script setup lang="ts">
import { computed, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import type { AdminUser, Dataset } from '../types'

import { api } from '../api'

const props = defineProps<{ current: AdminUser | null; admins: AdminUser[]; datasets: Dataset[] }>()
const emit = defineEmits<{
  create: [f: { username: string; password: string; role: string; dataset_ids: number[] }]
  update: [id: number, f: { role: string; dataset_ids: number[] }]
  delete: [id: number]
}>()

const searchQuery = ref('')
const showPwdModal = ref(false)
const pwdForm = ref({ current_password: '', new_password: '', confirm_password: '' })
const pwdLoading = ref(false)
const pwdError = ref('')
const pwdSuccess = ref('')
const resetPwdTarget = ref<AdminUser | null>(null)
const resetPwdForm = ref({ new_password: '', confirm_password: '' })
const resetPwdLoading = ref(false)
const resetPwdError = ref('')

async function doChangePassword() {
  pwdError.value = ''
  pwdSuccess.value = ''
  if (!pwdForm.value.current_password) { pwdError.value = '请输入当前密码'; return }
  if (pwdForm.value.new_password.length < 6) { pwdError.value = '新密码至少需要 6 位'; return }
  if (pwdForm.value.new_password !== pwdForm.value.confirm_password) { pwdError.value = '两次输入的新密码不一致'; return }
  pwdLoading.value = true
  try {
    await api.changePassword(pwdForm.value.current_password, pwdForm.value.new_password)
    pwdSuccess.value = '密码修改成功'
    pwdForm.value = { current_password: '', new_password: '', confirm_password: '' }
  } catch (err: any) {
    pwdError.value = err.message || '密码修改失败'
  } finally {
    pwdLoading.value = false
  }
}

function openResetPwd(admin: AdminUser) {
  resetPwdTarget.value = admin
  resetPwdForm.value = { new_password: '', confirm_password: '' }
  resetPwdError.value = ''
}

async function doResetPassword() {
  resetPwdError.value = ''
  if (resetPwdForm.value.new_password.length < 6) { resetPwdError.value = '新密码至少需要 6 位'; return }
  if (resetPwdForm.value.new_password !== resetPwdForm.value.confirm_password) { resetPwdError.value = '两次输入的新密码不一致'; return }
  resetPwdLoading.value = true
  try {
    await api.resetUserPassword(resetPwdTarget.value!.id, resetPwdForm.value.new_password)
    resetPwdTarget.value = null
  } catch (err: any) {
    resetPwdError.value = err.message || '重置密码失败'
  } finally {
    resetPwdLoading.value = false
  }
}

const showModal = ref(false)
const modalMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)
const editingUser = ref<AdminUser | null>(null)
const form = ref({ username: '', password: '', role: 'data_analyst', dataset_ids: [] as number[] })
const loading = ref(false)
const deleteTarget = ref<AdminUser | null>(null)

const ROLES = [
  { value: 'initial_admin', label: '超级管理员', color: '#dc2626', bg: '#fef2f2', border: '#fecaca' },
  { value: 'admin', label: '管理员', color: '#2563eb', bg: '#eff6ff', border: '#bfdbfe' },
  { value: 'data_analyst', label: '数据分析人员', color: '#0891b2', bg: '#ecfeff', border: '#a5f3fc' },
  { value: 'business_user', label: '业务人员', color: '#6b7280', bg: '#f9fafb', border: '#e5e7eb' },
]

const roleMap = computed(() => {
  const map: Record<string, typeof ROLES[0]> = {}
  for (const r of ROLES) map[r.value] = r
  return map
})

const createdByMap = computed(() => {
  const map: Record<number, string> = {}
  for (const a of props.admins) map[a.id] = a.username
  return map
})

const filteredAdmins = computed(() => {
  const q = searchQuery.value.toLowerCase().trim()
  if (!q) return props.admins
  return props.admins.filter(a =>
    a.username.toLowerCase().includes(q) ||
    ROLES.find(r => r.value === a.role)?.label?.toLowerCase().includes(q)
  )
})

function openCreate() {
  modalMode.value = 'create'
  editingId.value = null
  editingUser.value = null
  form.value = { username: '', password: '', role: 'data_analyst', dataset_ids: [] }
  showModal.value = true
}

function openEdit(admin: AdminUser) {
  modalMode.value = 'edit'
  editingId.value = admin.id
  editingUser.value = admin
  form.value = {
    username: admin.username,
    password: '',
    role: admin.role,
    dataset_ids: [...admin.dataset_permissions],
  }
  showModal.value = true
}

function closeModal() {
  showModal.value = false
}

function toggleDatasetId(id: number) {
  const idx = form.value.dataset_ids.indexOf(id)
  if (idx === -1) {
    form.value.dataset_ids.push(id)
  } else {
    form.value.dataset_ids.splice(idx, 1)
  }
}

async function submit() {
  if (modalMode.value === 'create') {
    if (!form.value.username || !form.value.password) return
    loading.value = true
    emit('create', {
      username: form.value.username,
      password: form.value.password,
      role: form.value.role,
      dataset_ids: form.value.dataset_ids,
    })
  } else {
    if (editingId.value === null) return
    loading.value = true
    emit('update', editingId.value, {
      role: form.value.role,
      dataset_ids: form.value.dataset_ids,
    })
  }
  loading.value = false
  showModal.value = false
}

function confirmDelete(admin: AdminUser) {
  deleteTarget.value = admin
}

function doDelete() {
  if (deleteTarget.value) {
    emit('delete', deleteTarget.value.id)
    deleteTarget.value = null
  }
}

function cancelDelete() {
  deleteTarget.value = null
}
</script>

<template>
  <section class="page account-page">
    <!-- Page Header -->
    <div class="account-page-header">
      <div class="account-page-title">
        <div class="page-intro" style="margin-bottom:0">
          <div>
            <span class="eyebrow"><AppIcon name="settings" :size="16" /> ACCOUNT MANAGEMENT</span>
            <h2>账户管理</h2>
            <p>管理系统用户账号、角色权限和数据访问范围</p>
          </div>
        </div>
      </div>
      <div class="account-header-actions">
        <button class="secondary-btn account-add-btn" @click="showPwdModal = true">
          <AppIcon name="settings" :size="14" /> 修改密码
        </button>
        <button v-if="current?.is_initial_admin" class="primary-btn account-add-btn" @click="openCreate">
          <AppIcon name="check" :size="14" /> 新增用户
        </button>
      </div>
    </div>

    <!-- Main Content Card -->
    <div class="account-card">
      <!-- Toolbar -->
      <div class="account-toolbar">
        <div class="account-search">
          <svg class="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input v-model="searchQuery" placeholder="搜索用户名或角色..." class="account-search-input" />
          <span v-if="searchQuery" class="search-clear" @click="searchQuery = ''">&times;</span>
        </div>
        <div class="account-stats">
          <span class="account-count">共 <strong>{{ filteredAdmins.length }}</strong> 个账户</span>
        </div>
      </div>

      <!-- Table -->
      <div class="table-scroll account-table-wrap">
        <table class="account-table">
          <thead>
            <tr>
              <th style="width:60px">ID</th>
              <th>用户名</th>
              <th style="width:140px">角色</th>
              <th style="width:160px">数据权限</th>
              <th style="width:110px">创建者</th>
              <th style="width:160px">创建时间</th>
              <th style="width:120px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="filteredAdmins.length === 0">
              <td colspan="7" class="account-empty-cell">
                <div class="account-empty">
                  <AppIcon name="database" :size="28" />
                  <p>{{ searchQuery ? '没有匹配的账户' : '暂无账户数据' }}</p>
                </div>
              </td>
            </tr>
            <tr v-for="admin in filteredAdmins" :key="admin.id">
              <td class="cell-id">{{ admin.id }}</td>
              <td>
                <div class="cell-user">
                  <span class="user-avatar" :style="{ background: roleMap[admin.role]?.bg || '#f0f4f8', color: roleMap[admin.role]?.color || '#718096' }">
                    {{ admin.username.charAt(0).toUpperCase() }}
                  </span>
                  <div>
                    <strong>{{ admin.username }}</strong>
                    <small v-if="admin.is_initial_admin" class="tag-initial">初始管理员</small>
                  </div>
                </div>
              </td>
              <td>
                <span class="role-badge" :style="{
                  color: roleMap[admin.role]?.color || '#718096',
                  background: roleMap[admin.role]?.bg || '#f9fafb',
                  borderColor: roleMap[admin.role]?.border || '#e5e7eb',
                }">
                  {{ roleMap[admin.role]?.label || admin.role }}
                </span>
              </td>
              <td>
                <div class="cell-perms">
                  <template v-if="admin.dataset_permissions.length === 0">
                    <span class="perm-all">全部数据集</span>
                  </template>
                  <template v-else>
                    <span v-for="did in admin.dataset_permissions" :key="did" class="perm-tag">
                      {{ datasets.find(d => d.id === did)?.name || `#${did}` }}
                    </span>
                  </template>
                </div>
              </td>
              <td>
                <span v-if="admin.created_by" class="cell-creator">{{ createdByMap[admin.created_by] || `用户#${admin.created_by}` }}</span>
                <span v-else class="cell-none">—</span>
              </td>
              <td class="cell-time">{{ admin.created_at }}</td>
              <td>
                <div class="cell-actions">
                  <button class="action-btn edit-btn" title="编辑" @click="openEdit(admin)">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                  </button>
                  <button
                    v-if="current?.is_initial_admin && !admin.is_initial_admin"
                    class="action-btn pwd-btn"
                    title="重置密码"
                    @click="openResetPwd(admin)"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                  </button>
                  <button
                    v-if="current?.is_initial_admin && !admin.is_initial_admin"
                    class="action-btn delete-btn"
                    title="删除"
                    @click="confirmDelete(admin)"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── Create / Edit Modal ── -->
    <Teleport to="body">
      <div v-if="showModal" class="modal-overlay" @click.self="closeModal">
        <div class="modal-panel">
          <div class="modal-header">
            <h3>{{ modalMode === 'create' ? '新增用户' : '编辑用户' }}</h3>
            <button class="modal-close" @click="closeModal">&times;</button>
          </div>
          <div class="modal-body">
            <!-- Username (create only) -->
            <div v-if="modalMode === 'create'" class="form-group">
              <label>用户名 <span class="required">*</span></label>
              <input v-model="form.username" placeholder="请输入用户名" autocomplete="off" />
            </div>
            <!-- Password (create only) -->
            <div v-if="modalMode === 'create'" class="form-group">
              <label>密码 <span class="required">*</span></label>
              <input v-model="form.password" type="password" placeholder="请输入密码（至少 6 位）" autocomplete="new-password" />
            </div>
            <!-- Role -->
            <div class="form-group">
              <label>角色</label>
              <div class="role-select-grid">
                <button
                  v-for="role in ROLES"
                  :key="role.value"
                  :class="['role-option', { selected: form.role === role.value }]"
                  :style="form.role === role.value ? { borderColor: role.color, background: role.bg } : {}"
                  @click="form.role = role.value"
                >
                  <span class="role-option-dot" :style="{ background: role.color }"></span>
                  <span class="role-option-label">{{ role.label }}</span>
                </button>
              </div>
            </div>
            <!-- Dataset Permissions -->
            <div class="form-group">
              <label>数据访问权限 <span class="form-hint">（不选则拥有全部数据集权限）</span></label>
              <div class="dataset-check-grid">
                <label
                  v-for="ds in datasets"
                  :key="ds.id"
                  :class="['dataset-check-item', { checked: form.dataset_ids.includes(ds.id) }]"
                >
                  <input
                    type="checkbox"
                    :checked="form.dataset_ids.includes(ds.id)"
                    @change="toggleDatasetId(ds.id)"
                  />
                  <span>{{ ds.name }}</span>
                </label>
                <div v-if="datasets.length === 0" class="form-hint" style="padding:8px">暂无可用数据集</div>
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button class="modal-btn cancel" @click="closeModal">取消</button>
            <button class="modal-btn primary" :disabled="loading" @click="submit">
              {{ modalMode === 'create' ? '确认创建' : '保存修改' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ── Delete Confirmation Modal ── -->
    <Teleport to="body">
      <div v-if="deleteTarget" class="modal-overlay" @click.self="cancelDelete">
        <div class="modal-panel modal-sm">
          <div class="modal-header">
            <h3>确认删除</h3>
            <button class="modal-close" @click="cancelDelete">&times;</button>
          </div>
          <div class="modal-body" style="text-align:center">
            <div class="delete-warn-icon">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            </div>
            <p class="delete-warn-text">确定要删除用户 <strong>{{ deleteTarget.username }}</strong> 吗？</p>
            <p class="delete-warn-sub">此操作不可撤销，该用户将无法再登录系统。</p>
          </div>
          <div class="modal-footer">
            <button class="modal-btn cancel" @click="cancelDelete">取消</button>
            <button class="modal-btn danger" @click="doDelete">确认删除</button>
          </div>
        </div>
      </div>
      <!-- ── Change Own Password Modal ── -->
      <div v-if="showPwdModal" class="modal-overlay" @click.self="showPwdModal = false">
        <div class="modal-panel">
          <div class="modal-header">
            <h3>修改密码</h3>
            <button class="modal-close" @click="showPwdModal = false">&times;</button>
          </div>
          <div class="modal-body">
            <div v-if="pwdError" class="form-error">{{ pwdError }}</div>
            <div v-if="pwdSuccess" class="form-success">{{ pwdSuccess }}</div>
            <div class="form-group">
              <label>当前密码 <span class="required">*</span></label>
              <input v-model="pwdForm.current_password" type="password" placeholder="请输入当前密码" autocomplete="current-password" />
            </div>
            <div class="form-group">
              <label>新密码 <span class="required">*</span></label>
              <input v-model="pwdForm.new_password" type="password" placeholder="请输入新密码（至少 6 位）" autocomplete="new-password" />
            </div>
            <div class="form-group">
              <label>确认新密码 <span class="required">*</span></label>
              <input v-model="pwdForm.confirm_password" type="password" placeholder="请再次输入新密码" autocomplete="new-password" />
            </div>
          </div>
          <div class="modal-footer">
            <button class="modal-btn cancel" @click="showPwdModal = false">取消</button>
            <button class="modal-btn primary" :disabled="pwdLoading" @click="doChangePassword">{{ pwdLoading ? '修改中...' : '确认修改' }}</button>
          </div>
        </div>
      </div>

      <!-- ── Reset User Password Modal ── -->
      <div v-if="resetPwdTarget" class="modal-overlay" @click.self="resetPwdTarget = null">
        <div class="modal-panel">
          <div class="modal-header">
            <h3>重置密码 — {{ resetPwdTarget.username }}</h3>
            <button class="modal-close" @click="resetPwdTarget = null">&times;</button>
          </div>
          <div class="modal-body">
            <div v-if="resetPwdError" class="form-error">{{ resetPwdError }}</div>
            <div class="form-group">
              <label>新密码 <span class="required">*</span></label>
              <input v-model="resetPwdForm.new_password" type="password" placeholder="请输入新密码（至少 6 位）" autocomplete="new-password" />
            </div>
            <div class="form-group">
              <label>确认新密码 <span class="required">*</span></label>
              <input v-model="resetPwdForm.confirm_password" type="password" placeholder="请再次输入新密码" autocomplete="new-password" />
            </div>
          </div>
          <div class="modal-footer">
            <button class="modal-btn cancel" @click="resetPwdTarget = null">取消</button>
            <button class="modal-btn primary" :disabled="resetPwdLoading" @click="doResetPassword">{{ resetPwdLoading ? '重置中...' : '确认重置' }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
/* ── Page Header ── */
.account-page { padding-top: 24px; }
.account-page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.account-page-title { flex: 1; }
.account-header-actions {
  display: flex;
  gap: 10px;
}
.account-add-btn {
  width: auto !important;
  padding: 10px 20px !important;
  margin-top: 0 !important;
  white-space: nowrap;
  font-size: 13px !important;
  flex-shrink: 0;
}
.form-error { color: #dc2626; font-size: 13px; padding: 8px 12px; background: #fef2f2; border-radius: 6px; margin-bottom: 10px; }
.form-success { color: #059669; font-size: 13px; padding: 8px 12px; background: #ecfdf5; border-radius: 6px; margin-bottom: 10px; }
.pwd-btn { color: #7c3aed; }
.pwd-btn:hover { background: #f5f3ff; color: #6d28d9; }
.modal-btn.primary { background: #2563eb; color: #fff; border: none; }
.modal-btn.primary:hover { background: #1d4ed8; }

/* ── Card ── */
.account-card {
  background: #fff;
  border: 1px solid #e3ebf2;
  border-radius: 14px;
  box-shadow: 0 4px 16px rgba(41, 72, 102, 0.04);
  overflow: hidden;
}

/* ── Toolbar ── */
.account-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid #edf2f6;
}
.account-search {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1;
  max-width: 340px;
}
.search-icon {
  position: absolute;
  left: 12px;
  color: #94a3b8;
  pointer-events: none;
}
.account-search-input {
  width: 100%;
  border: 1px solid #dce4ec;
  border-radius: 9px;
  padding: 9px 34px 9px 36px;
  font-size: 12px;
  color: #28465d;
  background: #f9fbfd;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.account-search-input:focus {
  border-color: #7cb9e8;
  box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.08);
  background: #fff;
}
.account-search-input::placeholder { color: #a8bac9; }
.search-clear {
  position: absolute;
  right: 8px;
  cursor: pointer;
  color: #94a3b8;
  font-size: 16px;
  line-height: 1;
  padding: 4px;
  border-radius: 4px;
}
.search-clear:hover { color: #64748b; background: #f1f5f9; }
.account-stats { flex-shrink: 0; }
.account-count { font-size: 11px; color: #8b9ba8; }
.account-count strong { color: #28465d; }

/* ── Table ── */
.account-table-wrap { max-height: 58vh; overflow: auto; }
.account-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.account-table thead { position: sticky; top: 0; z-index: 1; }
.account-table th {
  background: #f7fafc;
  color: #5d7386;
  text-align: left;
  font-weight: 700;
  font-size: 10px;
  letter-spacing: 0.4px;
  text-transform: uppercase;
  padding: 13px 16px;
  border-bottom: 2px solid #e8eef3;
  white-space: nowrap;
}
.account-table td {
  padding: 14px 16px;
  border-bottom: 1px solid #f0f4f8;
  color: #415a70;
  vertical-align: middle;
}
.account-table tbody tr { transition: background 0.15s; }
.account-table tbody tr:hover { background: #f8fbfd; }
.account-table tbody tr:last-child td { border-bottom: none; }

/* Cell styles */
.cell-id { color: #94a3b8; font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }
.cell-user { display: flex; align-items: center; gap: 10px; }
.user-avatar {
  width: 34px; height: 34px; border-radius: 9px;
  display: grid; place-items: center;
  font-weight: 700; font-size: 13px; flex-shrink: 0;
}
.cell-user strong { font-size: 12px; display: block; }
.cell-user small { display: inline-block; margin-top: 2px; }
.tag-initial {
  font-size: 9px; color: #b45309; background: #fffbeb;
  padding: 2px 6px; border-radius: 4px; font-weight: 600;
}
.cell-time { font-size: 11px; color: #7c8fa0; white-space: nowrap; }
.cell-creator { font-size: 11px; color: #506579; }
.cell-none { color: #bcc7d1; }
.cell-perms { display: flex; flex-wrap: wrap; gap: 4px; }
.perm-tag {
  font-size: 10px; color: #2d6a8f; background: #edf6ff;
  padding: 3px 7px; border-radius: 5px;
  border: 1px solid #d4e8f8;
  max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.perm-all {
  font-size: 10px; color: #0d8d69; background: #e6faf1;
  padding: 3px 7px; border-radius: 5px;
}

/* Role badge */
.role-badge {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid;
  white-space: nowrap;
}

/* Action buttons */
.cell-actions { display: flex; gap: 4px; }
.action-btn {
  width: 30px; height: 30px;
  border: 1px solid #e5e7eb;
  background: #fff;
  border-radius: 7px;
  display: grid; place-items: center;
  cursor: pointer;
  transition: all 0.15s;
  color: #6b7280;
}
.action-btn:hover { border-color: #d1d5db; background: #f9fafb; }
.edit-btn:hover { color: #2563eb; border-color: #bfdbfe; background: #eff6ff; }
.delete-btn:hover { color: #dc2626; border-color: #fecaca; background: #fef2f2; }

/* Empty state */
.account-empty-cell { text-align: center !important; padding: 48px 16px !important; }
.account-empty { display: flex; flex-direction: column; align-items: center; gap: 10px; color: #bcc7d1; }
.account-empty p { margin: 0; font-size: 12px; }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(13, 38, 62, 0.45);
  backdrop-filter: blur(4px);
  display: grid; place-items: center;
  z-index: 100;
  padding: 20px;
}
.modal-panel {
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 24px 60px rgba(13, 38, 62, 0.2);
  width: min(480px, 100%);
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  animation: modal-in 0.2s ease-out;
}
.modal-sm { width: min(400px, 100%); }
@keyframes modal-in {
  from { opacity: 0; transform: translateY(-12px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px;
  border-bottom: 1px solid #edf2f6;
}
.modal-header h3 { margin: 0; font-size: 16px; color: #18324a; }
.modal-close {
  border: none; background: #f1f5f9;
  width: 30px; height: 30px; border-radius: 8px;
  font-size: 18px; color: #64748b;
  display: grid; place-items: center;
  transition: all 0.15s;
}
.modal-close:hover { background: #e2e8f0; color: #334155; }
.modal-body {
  padding: 20px 22px;
  overflow-y: auto;
  flex: 1;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 22px;
  border-top: 1px solid #edf2f6;
}

/* Form */
.form-group { margin-bottom: 16px; }
.form-group:last-child { margin-bottom: 0; }
.form-group label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: #334155;
  margin-bottom: 7px;
}
.required { color: #dc2626; }
.form-hint { font-weight: 400; color: #94a3b8; font-size: 11px; }
.form-group input[type="text"],
.form-group input[type="password"] {
  width: 100%;
  border: 1px solid #d4dde6;
  border-radius: 9px;
  padding: 10px 12px;
  font-size: 12px;
  color: #1e3a54;
  background: #fafcfd;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.form-group input:focus {
  border-color: #7cb9e8;
  box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.08);
  background: #fff;
}

/* Role select grid */
.role-select-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.role-option {
  border: 1.5px solid #e2e8f0;
  background: #fff;
  border-radius: 9px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  transition: all 0.15s;
  font-size: 12px;
  color: #475569;
  text-align: left;
}
.role-option:hover { border-color: #cbd5e1; background: #f8fafc; }
.role-option.selected { border-width: 2px; font-weight: 600; }
.role-option-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.role-option-label { white-space: nowrap; }

/* Dataset checkboxes */
.dataset-check-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.dataset-check-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 7px;
  font-size: 11px;
  color: #475569;
  cursor: pointer;
  transition: all 0.15s;
  user-select: none;
}
.dataset-check-item:hover { border-color: #cbd5e1; background: #f8fafc; }
.dataset-check-item.checked {
  border-color: #93c5fd;
  background: #eff6ff;
  color: #1e40af;
}
.dataset-check-item input { accent-color: #2563eb; }

/* Modal buttons */
.modal-btn {
  border: none;
  border-radius: 9px;
  padding: 9px 20px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.modal-btn.cancel {
  background: #f1f5f9;
  color: #475569;
}
.modal-btn.cancel:hover { background: #e2e8f0; }
.modal-btn.primary {
  background: linear-gradient(110deg, #1677ff, #0eafd0);
  color: #fff;
  box-shadow: 0 4px 12px rgba(22, 119, 255, 0.2);
}
.modal-btn.primary:hover { box-shadow: 0 6px 16px rgba(22, 119, 255, 0.3); }
.modal-btn.primary:disabled { opacity: 0.45; cursor: not-allowed; box-shadow: none; }
.modal-btn.danger {
  background: #dc2626;
  color: #fff;
  box-shadow: 0 4px 12px rgba(220, 38, 38, 0.2);
}
.modal-btn.danger:hover { background: #b91c1c; box-shadow: 0 6px 16px rgba(220, 38, 38, 0.3); }

/* Delete warning */
.delete-warn-icon { margin-bottom: 12px; }
.delete-warn-text { font-size: 13px; color: #334155; margin: 0 0 6px; }
.delete-warn-sub { font-size: 11px; color: #94a3b8; margin: 0; }
</style>
