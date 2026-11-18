<template>
  <section class="page" data-module="sample_group">
    <header class="page-head">
      <div>
        <h2>样本分群管理</h2>
        <p class="page-desc">维护样本分群的输入、执行约束与结果记录，为样本分群提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记样本分群</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出样本分群清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>成员数量</th>
          <th>筛选条件版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :data-testid="`member-count-${row.id}`">{{ row.member_count ?? 0 }}</td>
          <td>
            <template v-if="row.filter_version_no">
              v{{ row.filter_version_no }}（{{ row.filter_version_status }}）
            </template>
            <span v-else>—</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`open-members-${row.id}`"
              @click="openMembers(row)"
            >
              成员明细
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无样本分群数据，可先登记样本分群</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记样本分群</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="drawer.group" class="modal-backdrop" data-testid="member-drawer">
      <aside class="modal member-drawer" role="dialog" aria-label="分群成员明细抽屉">
        <header class="page-head">
          <div>
            <h3>{{ drawer.group['分群名称'] }} · 成员明细</h3>
            <p class="page-desc">
              分群编号 {{ drawer.group['分群编号'] }} ｜
              已落库成员 {{ drawer.group.member_count ?? 0 }} ｜
              当前条件版本
              <template v-if="drawer.group.filter_version_no">v{{ drawer.group.filter_version_no }}</template>
              <template v-else>未计算</template>
            </p>
          </div>
          <div class="page-actions">
            <button class="btn ghost" type="button" data-testid="close-drawer" @click="closeMembers">关闭</button>
          </div>
        </header>

        <section class="filter-bar">
          <label class="filter-item">
            <span>条件版本回查</span>
            <select v-model.number="drawer.versionNo" data-testid="version-select" @change="loadMembers(1)">
              <option :value="null">当前版本</option>
              <option v-for="version in drawer.versions" :key="String(version.id)" :value="version.version_no">
                v{{ version.version_no }}（{{ version.status }}｜{{ version.member_count }} 人）
              </option>
            </select>
          </label>
          <button class="btn" type="button" data-testid="resume-members" @click="resumeMembers">中断恢复</button>
          <button class="btn" type="button" data-testid="reconcile-members" @click="reconcileMembers">残留核对</button>
          <button class="btn" type="button" data-testid="publish-new-first" @click="publishMembers('新成员优先')">发布：新成员优先</button>
          <button class="btn" type="button" data-testid="publish-old-first" @click="publishMembers('旧规则优先')">发布：旧规则优先</button>
          <button class="btn primary" type="button" data-testid="switch-version" @click="switchVersion">切换待切换版本</button>
        </section>

        <table class="data-table">
          <thead>
            <tr>
              <th>用户编号</th>
              <th>分组类型</th>
              <th>指标值</th>
              <th>筛选条件版本</th>
              <th>计算批次</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="member in drawer.members" :key="String(member.id)" :data-testid="`member-row-${member.user_id}`">
              <td>{{ member.user_id }}</td>
              <td>{{ member.group_type }}</td>
              <td>{{ member.metric_value }}</td>
              <td>
                v{{ member.filter_version_no }}
                <span class="version-digest">{{ member.condition_digest }}</span>
              </td>
              <td>{{ member.batch_no }}</td>
            </tr>
            <tr v-if="!drawer.members.length">
              <td colspan="5" class="empty-state">该版本暂无成员明细，请先执行「开始计算」或「中断恢复」</td>
            </tr>
          </tbody>
        </table>

        <footer class="page-foot">
          <span>共 {{ drawer.total }} 条成员记录，第 {{ drawer.page }} 页</span>
          <span class="page-actions">
            <button class="btn" type="button" data-testid="member-prev" :disabled="drawer.page <= 1" @click="loadMembers(drawer.page - 1)">上一页</button>
            <button class="btn" type="button" data-testid="member-next" :disabled="drawer.page * drawer.size >= drawer.total" @click="loadMembers(drawer.page + 1)">下一页</button>
          </span>
          <span v-if="drawer.message" class="error-text">{{ drawer.message }}</span>
        </footer>
      </aside>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条样本分群记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Member = Record<string, string | number | null>
type VersionRow = Record<string, string | number | null>

const ENDPOINT = '/api/sample_group'
const columns = ["分群编号", "分群名称", "筛选条件", "样本基数", "分群日期", "更新方式", "有效天数", "负责人"]
const actions = ["开始计算", "确认就绪", "标记过期"]
const statuses = ["草稿", "计算中", "已就绪", "已过期"]
const metricNames = ["样本分群总数", "计算中分群", "已就绪分群"]
const PAGE_SIZE = 20

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

// 抽屉与导航页读取同一套已落库业务数据（成员数、条件版本均来自后端持久化结果）
const drawer = reactive<{
  group: Row | null
  members: Member[]
  versions: VersionRow[]
  total: number
  page: number
  size: number
  versionNo: number | null
  message: string
}>({
  group: null,
  members: [],
  versions: [],
  total: 0,
  page: 1,
  size: PAGE_SIZE,
  versionNo: null,
  message: '',
})

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'sample_group.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('样本分群动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message || '样本分群操作失败')
    await reload()
    if (drawer.group && String(drawer.group.id) === String(row.id)) {
      await openMembers(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样本分群操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('样本分群列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样本分群列表读取失败'
  }
}

async function openMembers(row: Row) {
  drawer.group = row
  drawer.versionNo = null
  drawer.page = 1
  drawer.message = ''
  drawer.members = []
  drawer.versions = []
  try {
    const versionResp = await request(`${ENDPOINT}/${row.id}/versions`)
    if (versionResp.ok) {
      const payload = await versionResp.json()
      drawer.versions = payload.items ?? []
    }
  } catch {
    drawer.versions = []
  }
  await loadMembers(1)
}

function closeMembers() {
  drawer.group = null
  drawer.members = []
  drawer.versions = []
  drawer.message = ''
}

async function loadMembers(page: number) {
  if (!drawer.group) return
  drawer.message = ''
  const params = new URLSearchParams({ page: String(page), size: String(drawer.size) })
  if (drawer.versionNo !== null) params.set('version_no', String(drawer.versionNo))
  try {
    const response = await request(`${ENDPOINT}/${drawer.group.id}/members?${params.toString()}`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '成员明细读取失败')
    drawer.members = payload.items ?? []
    drawer.total = payload.total ?? 0
    drawer.page = payload.page ?? page
  } catch (error) {
    drawer.message = error instanceof Error ? error.message : '成员明细读取失败'
    drawer.members = []
    drawer.total = 0
  }
}

async function postMemberAction(path: string, body: Record<string, unknown>, okMessage: string) {
  if (!drawer.group) return
  try {
    const response = await request(`${ENDPOINT}/${drawer.group.id}${path}`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '操作未生效')
    drawer.message = okMessage
    await reload()
    drawer.group = rows.value.find((row) => String(row.id) === String(drawer.group?.id)) ?? drawer.group
    const versionResp = await request(`${ENDPOINT}/${drawer.group.id}/versions`)
    if (versionResp.ok) {
      drawer.versions = (await versionResp.json()).items ?? []
    }
    await loadMembers(drawer.page)
  } catch (error) {
    drawer.message = error instanceof Error ? error.message : '操作失败'
  }
}

function resumeMembers() {
  void postMemberAction('/resume', {}, '中断成员已补齐，未重复写入同一用户记录')
}

function reconcileMembers() {
  void postMemberAction('/reconcile', {}, '残留核对完成')
}

function publishMembers(mode: string) {
  void postMemberAction('/publish', { action: mode }, `已按「${mode}」裁决发布次序`)
}

async function switchVersion() {
  await postMemberAction('/switch-version', {}, '已切换到待切换的新成员集合')
  drawer.versionNo = null
}

onMounted(reload)
</script>

<style scoped>
.member-drawer {
  width: min(960px, 96vw);
}
.version-digest {
  margin-left: 6px;
  color: var(--muted, #64748b);
  font-size: 12px;
}
</style>
