<template>
  <div class="min-w-0">
    <div class="mb-4 grid grid-cols-3 gap-3 max-md:gap-2">
      <div v-for="item in summary" :key="item.label" class="flex min-w-0 flex-col rounded-xl border border-border bg-card px-4 py-3 max-md:px-3">
        <div class="truncate text-[12px] text-muted-foreground">{{ item.label }}</div>
        <div class="num mt-1 truncate text-[18px] text-foreground max-md:text-[15px]">{{ item.value }}</div>
        <template v-if="item.trend">
          <Sparkline class="mt-2 h-9 w-full" :data="item.trend" :color="item.color" />
          <div class="num mt-1 truncate text-[11px] text-muted-foreground" :title="`近 2 分钟峰值 ${item.peak}`">近 2 分钟峰值 {{ item.peak }}</div>
        </template>
      </div>
    </div>

    <div class="mb-4 min-w-0 rounded-xl border border-border bg-card">
      <div class="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-3 py-2">
        <Segmented v-model="view" :options="viewOptions" label="连接状态" class="max-w-full overflow-x-auto" />
        <div class="ml-auto flex flex-wrap items-center gap-2">
          <label class="flex min-h-11 items-center gap-2 text-xs text-muted-foreground">
            <Switch v-model="live" aria-label="每 2 秒刷新连接" />实时
          </label>
          <Button v-if="view === 'closed'" variant="ghost" class="min-h-11" :disabled="!closed.length" @click="closed = []">
            <Eraser class="size-3.5" />清空记录
          </Button>
          <Button v-else variant="ghost" class="min-h-11" :disabled="!activeMatches.length || closingAll" @click="closeAll">
            <Loader2 v-if="closingAll" class="size-3.5 animate-spin" /><X v-else class="size-3.5" />{{ filtering ? '断开筛选结果' : '全部断开' }}
          </Button>
        </div>
      </div>
      <Toolbar v-model:search="search" class="mb-0 rounded-none border-0 bg-transparent" placeholder="搜索主机、IP、备注、规则，支持正则">
        <template #filters>
          <div class="col-span-full flex min-w-0 items-center gap-1.5 max-md:w-full">
            <Select :model-value="settings.sort" @update:model-value="value => changeSort(String(value) as ConnectionSort)">
              <SelectTrigger class="min-h-11 w-36 min-w-0 max-md:flex-1" aria-label="连接排序字段"><SelectValue /></SelectTrigger>
              <SelectContent class="max-h-80">
                <SelectItem v-for="option in CONNECTION_SORT_OPTIONS" :key="option.key" :value="option.key">{{ option.label }}</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="ghost" size="icon" class="size-11 shrink-0" :aria-label="settings.direction === 'asc' ? '当前升序，点击改为降序' : '当前降序，点击改为升序'"
              @click="settings.direction = settings.direction === 'asc' ? 'desc' : 'asc'">
              <ArrowUp v-if="settings.direction === 'asc'" class="size-4" /><ArrowDown v-else class="size-4" />
            </Button>
            <Popover>
              <PopoverTrigger as-child>
                <Button variant="outline" class="min-h-11 shrink-0 gap-1.5 px-3" :class="filterCount ? 'border-primary-accent text-primary-accent' : ''" :aria-label="`筛选连接${filterCount ? `，${filterCount} 项条件` : ''}`">
                  <ListFilter class="size-4" /><span>筛选</span><span v-if="filterCount" class="num text-xs">{{ filterCount }}</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" :collision-padding="12" class="w-[440px] max-w-[calc(100vw-24px)] max-h-(--reka-popover-content-available-height) overflow-y-auto p-4" role="dialog" aria-label="连接筛选" @open-auto-focus="focusFilters">
                <div class="mb-3 flex min-h-8 items-center justify-between gap-2">
                  <div class="flex items-center gap-1 text-sm font-semibold">筛选连接<FieldHelp label="连接筛选" text="同一项可多选，满足其中任意一个即可；不同项需要同时满足。链路可选择策略组或节点。搜索与隐藏条件均支持正则，也会匹配未显示的列。" /></div>
                  <div class="flex shrink-0 items-center gap-1">
                    <Button variant="ghost" class="min-h-11 px-2" :disabled="!filtering" @click="clearFilters">清除</Button>
                    <PopoverClose as-child><Button variant="secondary" class="min-h-11 px-3">完成</Button></PopoverClose>
                  </div>
                </div>
                <div class="connection-filter-fields grid min-w-0 grid-cols-1 gap-4 min-[400px]:grid-cols-2">
                  <FormField v-for="group in filterGroups" :key="group.key" :label="group.label" :html-for="`connection-filter-${group.key}`">
                    <MultiSelect v-model="filters[group.key]" :trigger-id="`connection-filter-${group.key}`" :options="group.options" :placeholder="`全部${group.label}`" :search-placeholder="`搜索${group.label}…`" :max-visible="1" />
                  </FormField>
                  <FormField class="col-span-full" label="隐藏匹配的连接" html-for="connection-exclude" help="符合这里内容的连接会从结果中隐藏。支持正则表达式，留空时不隐藏。">
                    <Input id="connection-exclude" v-model="filters.exclude" placeholder="例如：测速域名或进程名" class="min-h-11" />
                  </FormField>
                </div>
              </PopoverContent>
            </Popover>
            <Popover>
              <PopoverTrigger as-child>
                <Button variant="outline" size="icon" class="size-11 shrink-0" aria-label="设置显示列" title="显示列"><Columns3 class="size-4" /></Button>
              </PopoverTrigger>
              <PopoverContent align="end" :collision-padding="12" class="w-72 max-h-(--reka-popover-content-available-height) overflow-y-auto" role="dialog" aria-label="显示列">
                <div class="mb-2 flex items-center justify-between gap-2 text-sm font-semibold">显示列<Button variant="ghost" class="min-h-11 px-2 font-normal" @click="settings.columns = [...DEFAULT_CONNECTION_COLUMNS]">恢复默认</Button></div>
                <p class="mb-2 mt-0 text-xs leading-relaxed text-muted-foreground">相关信息合并显示；完整内容可点击连接查看。</p>
                <div class="grid grid-cols-2 gap-x-3">
                  <label v-for="column in CONNECTION_COLUMNS" :key="column.key" class="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]">
                    <Checkbox :model-value="settings.columns.includes(column.key)" :disabled="column.key === 'target'" @update:model-value="value => toggleColumn(column.key, !!value)" />{{ column.label }}
                  </label>
                </div>
              </PopoverContent>
            </Popover>
          </div>
        </template>
      </Toolbar>
      <div v-if="filtering" class="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-1 text-xs text-muted-foreground">
        <span>匹配 <span class="num text-foreground">{{ rows.length }}</span> 条连接<span v-if="filters.exclude.trim()"> · 已应用隐藏条件</span></span>
        <Button variant="ghost" class="min-h-11 px-2 text-xs" @click="clearFilters"><X class="size-3.5" />清除筛选</Button>
      </div>
    </div>

    <div v-if="notesError" role="alert" class="mb-3 flex flex-wrap items-center gap-2 text-xs text-warning-accent">
      <span>设备备注暂未读取：{{ notesError }}</span><Button variant="ghost" class="min-h-11 px-2" @click="loadNotes">重试</Button>
    </div>
    <p v-if="error && snapshot" role="alert" class="mb-4 rounded-lg border border-warning-accent/30 bg-warning-soft p-3 text-sm text-warning-accent">
      {{ error }} 以下为上次成功读取的连接。
    </p>
    <LoadingRows v-if="!snapshot && !error" :rows="6" />
    <SectionCard v-else-if="!snapshot">
      <EmptyState :icon="TriangleAlert" title="读取连接失败" :description="error">
        <Button variant="outline" class="min-h-11" @click="load">重试</Button>
      </EmptyState>
    </SectionCard>
    <SectionCard v-else-if="!rows.length">
      <EmptyState :icon="Cable" :title="filtering ? '没有匹配的连接' : view === 'closed' ? '暂无已关闭的连接' : '当前没有活动连接'"
        :description="view === 'closed' && !filtering ? '打开本页后关闭的连接会记录在这里（最近 500 条），切换设备或刷新页面后清空。' : undefined" />
    </SectionCard>
    <div v-else class="connection-table-frame min-w-0 overflow-hidden rounded-xl border border-border bg-card">
      <div class="connection-table-scroll max-h-[min(65dvh,640px)] overflow-y-auto overscroll-contain" tabindex="0" role="region" aria-label="连接列表，点击连接查看完整详情">
        <table class="cf-table connection-table table-fixed">
          <caption class="cf-sr">Mihomo 连接，点击表头切换排序，点击主机查看详情</caption>
          <thead>
            <tr>
              <th v-for="column in visibleColumns" :key="column.key" scope="col" :style="{ width: `${100 * column.weight / columnWeight}%` }" :class="{ 'cf-table__right': column.numeric }"
                :aria-sort="column.fields.some(field => field === settings.sort) ? settings.direction === 'asc' ? 'ascending' : 'descending' : 'none'">
                <button v-if="column.fields.length === 1" type="button" class="connection-sort" :class="{ 'justify-end': column.numeric }"
                  :aria-label="`按${columnLabel(column.fields[0])}排序`" @click="changeSort(column.fields[0], true)">
                  {{ column.label }}<ArrowUp v-if="settings.sort === column.fields[0] && settings.direction === 'asc'" class="size-3.5 shrink-0" /><ArrowDown v-else-if="settings.sort === column.fields[0]" class="size-3.5 shrink-0" /><ArrowUpDown v-else class="size-3 shrink-0 text-muted-foreground" />
                </button>
                <DropdownMenu v-else>
                  <DropdownMenuTrigger as-child>
                    <button type="button" class="connection-sort" :class="{ 'justify-end': column.numeric }" :aria-label="`${column.label}排序选项`">
                      {{ column.label }}<ArrowUp v-if="column.fields.some(field => field === settings.sort) && settings.direction === 'asc'" class="size-3.5 shrink-0" /><ArrowDown v-else-if="column.fields.some(field => field === settings.sort)" class="size-3.5 shrink-0" /><ArrowUpDown v-else class="size-3 shrink-0 text-muted-foreground" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent :align="column.numeric ? 'end' : 'start'">
                    <DropdownMenuItem v-for="field in column.fields" :key="field" class="min-h-11" :aria-label="`按${columnLabel(field)}排序`" @select="changeSort(field, true)">
                      {{ columnLabel(field) }}<ArrowUp v-if="settings.sort === field && settings.direction === 'asc'" class="ml-auto size-3.5" /><ArrowDown v-else-if="settings.sort === field" class="ml-auto size-3.5" />
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </th>
              <th class="connection-actions cf-table__right" scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in visibleRows" :key="row.id" :data-connection-id="row.id" class="cursor-pointer" :class="{ 'text-muted-foreground': row.closedAt }" @click="selectedId = row.id">
              <td v-for="column in visibleColumns" :key="column.key" :data-column="column.key" :data-label="column.label" class="text-xs" :class="{ 'cf-table__right num': column.numeric }">
                <template v-if="column.key === 'target'">
                  <button type="button" class="block min-h-11 w-full rounded-sm text-left font-mono text-xs text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                    :title="row.target" :aria-label="`查看 ${row.target} 的连接详情`" @click.stop="selectedId = row.id"><span class="connection-wrap">{{ row.target }}</span></button>
                  <div class="connection-wrap text-[11.5px] text-muted-foreground" :title="row.metadata.type">
                    <span v-if="row.closedAt" class="mr-1">已关闭 ·</span>{{ row.metadata.type }}<template v-if="column.fields.includes('network')"> · {{ connectionNetworkLabel(row) }}</template>
                  </div>
                  <div v-if="column.fields.includes('process')" class="connection-wrap mt-0.5 font-mono text-muted-foreground" :title="row.metadata.processPath || row.metadata.process">{{ row.metadata.process || row.metadata.processPath || '—' }}</div>
                </template>
                <div v-else-if="column.key === 'source'" :title="[sourceName(row), sourceLabel(row)].filter(Boolean).join(' · ')">
                  <div v-if="sourceName(row)" class="connection-wrap mb-0.5 text-foreground">{{ sourceName(row) }}</div>
                  <div class="connection-wrap font-mono text-muted-foreground">{{ sourceLabel(row) }}</div>
                </div>
                <div v-else-if="column.key === 'routing'" class="space-y-1">
                  <div v-if="column.fields.includes('rule')" class="connection-wrap" :title="connectionRuleLabel(row)">{{ connectionRuleLabel(row) }}</div>
                  <div v-if="column.fields.includes('chain')" class="connection-wrap text-muted-foreground" :title="row.chain">{{ row.chain || '—' }}</div>
                </div>
                <div v-else-if="column.key === 'transfer'" class="space-y-1">
                  <div v-for="field in column.fields" :key="field" class="connection-transfer flex items-baseline justify-end gap-1.5" :aria-label="`${columnLabel(field)} ${transferText(row, field)}`" :title="columnLabel(field)">
                    <span class="shrink-0 text-muted-foreground" aria-hidden="true">{{ field === 'download' || field === 'upload' ? (field === 'download' ? '下载' : '上传') : (field === 'downSpeed' ? '↓' : '↑') }}</span>
                    <span class="min-w-0 break-all">{{ transferText(row, field) }}</span>
                  </div>
                </div>
                <template v-else-if="column.key === 'time'">
                  <div v-if="column.fields.includes('duration')">{{ durationText(row.start, row.closedAt) }}</div>
                  <div v-if="column.fields.includes('start')" class="mt-0.5 break-words text-muted-foreground" :title="startText(row.start)">{{ startText(row.start) }}</div>
                </template>
              </td>
              <td class="connection-actions cf-table__right" @click.stop>
                <div class="flex items-center justify-end">
                <DropdownMenu>
                  <DropdownMenuTrigger as-child><Button variant="ghost" size="icon" class="size-11" :aria-label="`操作 ${row.target}`"><Ellipsis class="size-4" /></Button></DropdownMenuTrigger>
                  <DropdownMenuContent align="end" class="max-w-[calc(100vw-24px)]">
                    <DropdownMenuItem class="min-h-11" :disabled="!deviceAddress(row.metadata.sourceIP)" @select="noteIP = deviceAddress(row.metadata.sourceIP)"><Pencil class="size-4" />编辑来源设备备注</DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem class="min-h-11" :disabled="!connectionDomains(row.metadata).length" @select="openRule(row, 'proxy')">加入代理规则集</DropdownMenuItem>
                    <DropdownMenuItem class="min-h-11" :disabled="!connectionDomains(row.metadata).length" @select="openRule(row, 'direct')">加入直连规则集</DropdownMenuItem>
                    <DropdownMenuLabel v-if="!connectionDomains(row.metadata).length" class="max-w-64 whitespace-normal text-xs font-normal text-muted-foreground">此连接没有记录域名，不能添加到域名规则集。</DropdownMenuLabel>
                  </DropdownMenuContent>
                </DropdownMenu>
                <Button v-if="!row.closedAt" variant="ghost" size="icon" class="size-11" :aria-label="`断开 ${row.target}`" title="断开" @click.stop="closeOne(row.id)"><X class="size-4" /></Button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground">
        <span class="num">共 {{ rows.length }} 条<span v-if="pageCount > 1"> · {{ (page - 1) * PAGE_SIZE + 1 }}–{{ Math.min(page * PAGE_SIZE, rows.length) }}</span></span>
        <div v-if="pageCount > 1" class="flex items-center gap-1">
          <Button variant="ghost" size="icon" class="size-11" aria-label="上一页连接" :disabled="page === 1" @click="page--"><ChevronLeft class="size-4" /></Button>
          <span class="num px-1">{{ page }} / {{ pageCount }}</span>
          <Button variant="ghost" size="icon" class="size-11" aria-label="下一页连接" :disabled="page === pageCount" @click="page++"><ChevronRight class="size-4" /></Button>
        </div>
      </div>
    </div>
    <DeviceNoteDialog v-if="noteIP" :key="agentId + noteIP" :agent-id="agentId" :ip="noteIP" @close="noteIP = ''" @saved="applyNotes" />
    <ConnectionRuleDialog v-if="ruleAction" :key="agentId" :agent-id="agentId" :domains="ruleAction.domains" :target="ruleAction.target" @close="ruleAction = null" />
    <MihomoConnectionDetail :row="selected" :source-name="selected ? sourceName(selected) : ''" @close="selectedId = ''" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, ArrowUpDown, Cable, ChevronLeft, ChevronRight, Columns3, Ellipsis, Eraser, ListFilter, Loader2, Pencil, TriangleAlert, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { PopoverClose } from 'reka-ui'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Switch } from '@/components/ui/switch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import Toolbar from '@/components/common/Toolbar.vue'
import Segmented from '@/components/common/Segmented.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import Sparkline from '@/components/common/Sparkline.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import FormField from '@/components/common/FormField.vue'
import MultiSelect from '@/components/common/MultiSelect.vue'
import MihomoConnectionDetail from './MihomoConnectionDetail.vue'
import DeviceNoteDialog from './DeviceNoteDialog.vue'
import ConnectionRuleDialog from './ConnectionRuleDialog.vue'
import { connectionDomains, deviceAddress, deviceNote, type DeviceNotes } from '@/lib/device-notes'
import { clientToolsApi, mihomoPanelApi } from '@/api'
import { confirmDanger, notify } from '@/lib/feedback'
import { formatBytes } from '@/lib/format'
import {
  compareSources, panelError, sourceLabel, type ConnectionRow, type MihomoConnection, type MihomoConnectionsSnapshot
} from '@/types/mihomo'
import {
  CONNECTION_COLUMNS, CONNECTION_SORT_OPTIONS, DEFAULT_CONNECTION_COLUMNS,
  connectionDisplayColumns, connectionNetworkLabel, connectionRuleLabel, defaultConnectionSortDirection, emptyConnectionFilters,
  filterConnections, loadConnectionTableSettings, saveConnectionTableSettings, sortConnections,
  type ConnectionColumn, type ConnectionSort
} from '@/lib/mihomo-connections'

type ViewKey = 'active' | 'closed' | 'all'
const POLL_MS = 2000
const PAGE_SIZE = 100
const CLOSED_LIMIT = 500
/** 趋势图保留的采样数：2 秒一次，约 2 分钟 */
const TREND_POINTS = 60

const props = defineProps<{ agentId: string; active: boolean }>()

const notes = ref<Record<string, string>>({})
const notesError = ref('')
const noteIP = ref('')
const ruleAction = ref<{ domains: string[]; target: 'proxy' | 'direct' } | null>(null)
const sourceName = (row: ConnectionRow) => deviceNote(notes.value, row.metadata.sourceIP)
let notesEpoch = 0
function applyNotes(catalog: DeviceNotes) {
  notesEpoch++; notes.value = catalog.notes; notesError.value = ''
}
async function loadNotes() {
  const generation = ++notesEpoch
  const agentId = props.agentId
  try {
    const { data } = await clientToolsApi.notes(agentId)
    if (generation !== notesEpoch || disposed) return
    if (!data?.success) throw new Error('读取备注失败。')
    notes.value = data.notes; notesError.value = ''
  } catch (failure) {
    if (generation === notesEpoch && !disposed) notesError.value = panelError(failure)
  }
}
function openRule(row: ConnectionRow, target: 'proxy' | 'direct') {
  const domains = connectionDomains(row.metadata)
  if (domains.length) ruleAction.value = { domains, target }
}
const snapshot = ref<MihomoConnectionsSnapshot | null>(null)
const error = ref('')
const search = ref('')
const settings = ref(loadConnectionTableSettings())
const filters = ref(emptyConnectionFilters())
const view = ref<ViewKey>('active')
const page = ref(1)
const selectedId = ref('')
/** 两次采样之间消失的连接，按关闭时间倒序，只保存在本页 */
const closed = ref<ConnectionRow[]>([])
let lastById = new Map<string, MihomoConnection>()
const trend = ref<{ down: number[]; up: number[]; memory: number[] }>({ down: [], up: [], memory: [] })
const live = ref(true)
const closingAll = ref(false)
const now = ref(Date.now())
const speeds = ref(new Map<string, { up: number; down: number }>())
const totalSpeed = ref({ up: 0, down: 0 })
let previous: { at: number; totals: { up: number; down: number }; byId: Map<string, { up: number; down: number }> } | null = null

function toRow(connection: MihomoConnection, closedAt?: number): ConnectionRow {
  const meta = connection.metadata
  const host = meta.host || meta.sniffHost || meta.destinationIP
  const speed = closedAt ? undefined : speeds.value.get(connection.id)
  return {
    ...connection,
    raw: connection,
    target: `${host}:${meta.destinationPort}`,
    chain: [...connection.chains].reverse().join(' → '),
    upSpeed: speed?.up || 0,
    downSpeed: speed?.down || 0,
    closedAt
  }
}

const activeRows = computed(() => (snapshot.value?.connections || []).map(connection => toRow(connection)))
const filterCount = computed(() => [filters.value.sources, filters.value.networks, filters.value.rules, filters.value.chains].filter(values => values.length).length + Number(!!filters.value.exclude.trim()))
const filtering = computed(() => !!search.value.trim() || filterCount.value > 0)
const sources = computed(() => [...new Set([...activeRows.value, ...closed.value].map(sourceLabel))].sort(compareSources))
const filterGroups = computed(() => {
  const all = [...activeRows.value, ...closed.value]
  // 保留已选但当前没有连接的选项，不能在刷新时悄悄放宽筛选范围。
  const options = (values: string[], selected: string[], compare = (a: string, b: string) => a.localeCompare(b)) =>
    [...new Set([...values, ...selected])].filter(Boolean).sort(compare).map(value => ({ value, label: value }))
  return [
    { key: 'sources' as const, label: '来源', options: options(sources.value, filters.value.sources, compareSources).map(option => ({ ...option, label: deviceNote(notes.value, option.value) ? `${deviceNote(notes.value, option.value)} · ${option.value}` : option.label })) },
    { key: 'networks' as const, label: '协议', options: options(all.map(connectionNetworkLabel), filters.value.networks) },
    { key: 'rules' as const, label: '规则', options: options(all.map(connectionRuleLabel), filters.value.rules) },
    { key: 'chains' as const, label: '链路', options: options(all.flatMap(row => row.chains), filters.value.chains) }
  ]
})

function clearFilters() {
  filters.value = emptyConnectionFilters()
  search.value = ''
}
function focusFilters(event: Event) {
  event.preventDefault()
  document.getElementById('connection-filter-sources')?.focus()
}
const activeMatches = computed(() => filterConnections(activeRows.value, search.value, filters.value, sourceName))

const rows = computed(() => {
  const list = view.value === 'active' ? activeMatches.value
    : filterConnections(view.value === 'closed' ? closed.value : [...activeRows.value, ...closed.value], search.value, filters.value, sourceName)
  return sortConnections(list, settings.value.sort, settings.value.direction, now.value)
})
const visibleColumns = computed(() => connectionDisplayColumns(settings.value.columns))
const columnWeight = computed(() => visibleColumns.value.reduce((sum, column) => sum + column.weight, 0))
const columnLabel = (key: ConnectionColumn) => CONNECTION_COLUMNS.find(column => column.key === key)!.label
function transferText(row: ConnectionRow, key: ConnectionColumn) {
  if (key === 'downSpeed' || key === 'upSpeed') return row.closedAt ? '—' : speedText(row[key])
  return key === 'download' || key === 'upload' ? formatBytes(row[key]) : '—'
}
function changeSort(key: ConnectionSort, toggle = false) {
  settings.value.direction = toggle && settings.value.sort === key
    ? settings.value.direction === 'asc' ? 'desc' : 'asc'
    : defaultConnectionSortDirection(key)
  settings.value.sort = key
}
function toggleColumn(key: ConnectionColumn, visible: boolean) {
  if (key === 'target') return
  settings.value.columns = CONNECTION_COLUMNS.filter(column => column.key === key ? visible : settings.value.columns.includes(column.key)).map(column => column.key)
}
const viewOptions = computed<Array<{ value: ViewKey; label: string }>>(() => [
  { value: 'active', label: `活跃 (${activeRows.value.length})` },
  { value: 'closed', label: `已关闭 (${closed.value.length})` },
  { value: 'all', label: '全部' }
])
const selected = computed(() => selectedId.value
  ? activeRows.value.find(row => row.id === selectedId.value) || closed.value.find(row => row.id === selectedId.value) || null
  : null)
const pageCount = computed(() => Math.max(1, Math.ceil(rows.value.length / PAGE_SIZE)))
const visibleRows = computed(() => rows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))
watch([search, view, filters, () => settings.value.sort, () => settings.value.direction], () => { page.value = 1 }, { deep: true })
watch(pageCount, count => { page.value = Math.min(page.value, count) })
watch(settings, saveConnectionTableSettings, { deep: true })

const peak = (values: number[]) => values.length ? Math.max(...values) : 0
// 上排是计数与累计量，下排是带趋势线的实时指标，两排高度各自一致
const summary = computed(() => [
  { label: '连接', value: String(snapshot.value?.connections?.length ?? '—') },
  { label: '累计上传', value: formatBytes(snapshot.value?.uploadTotal) },
  { label: '累计下载', value: formatBytes(snapshot.value?.downloadTotal) },
  {
    label: '内存使用', value: formatBytes(snapshot.value?.memory),
    trend: trend.value.memory, color: '--warning-accent', peak: formatBytes(peak(trend.value.memory))
  },
  {
    label: '上传速度', value: speedText(totalSpeed.value.up),
    trend: trend.value.up, color: '--success-accent', peak: speedText(peak(trend.value.up))
  },
  {
    label: '下载速度', value: speedText(totalSpeed.value.down),
    trend: trend.value.down, color: '--info-accent', peak: speedText(peak(trend.value.down))
  }
])

function speedText(bytesPerSecond: number) {
  return bytesPerSecond > 0 ? formatBytes(bytesPerSecond) + '/s' : '—'
}

function durationText(start: string, end = now.value) {
  const seconds = Math.max(0, Math.floor((end - Date.parse(start)) / 1000))
  if (!Number.isFinite(seconds)) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

function startText(start: string) {
  return Number.isFinite(Date.parse(start)) ? new Date(start).toLocaleString() : '—'
}

/** 返回本次是否算出了速率（需要有上一个样本） */
function updateSpeeds(data: MihomoConnectionsSnapshot): boolean {
  const measured = previous !== null
  const at = Date.now()
  const byId = new Map((data.connections || []).map(c => [c.id, { up: c.upload, down: c.download }]))
  const totals = { up: data.uploadTotal, down: data.downloadTotal }
  const next = new Map<string, { up: number; down: number }>()
  if (previous) {
    const seconds = Math.max((at - previous.at) / 1000, 0.5)
    for (const [id, current] of byId) {
      const before = previous.byId.get(id)
      if (before) next.set(id, { up: Math.max(0, (current.up - before.up) / seconds), down: Math.max(0, (current.down - before.down) / seconds) })
    }
    totalSpeed.value = { up: Math.max(0, (totals.up - previous.totals.up) / seconds), down: Math.max(0, (totals.down - previous.totals.down) / seconds) }
  }
  speeds.value = next
  previous = { at, totals, byId }
  now.value = at
  return measured
}

let timer: ReturnType<typeof setTimeout> | null = null
let controller: AbortController | null = null
let disposed = false

async function load() {
  controller?.abort()
  controller = new AbortController()
  try {
    const { data } = await mihomoPanelApi.connections(props.agentId, { signal: controller.signal })
    const result: MihomoConnectionsSnapshot = data.data || { connections: [], uploadTotal: 0, downloadTotal: 0 }
    const measured = updateSpeeds(result)
    recordClosed(result.connections || [])
    recordTrend(result, measured)
    snapshot.value = result
    error.value = ''
  } catch (failure: any) {
    if (failure?.code === 'ERR_CANCELED') return
    error.value = panelError(failure, '暂时无法读取连接。')
  }
}

function recordTrend(data: MihomoConnectionsSnapshot, hasSpeed: boolean) {
  const push = (values: number[], value: number) => [...values, value].slice(-TREND_POINTS)
  trend.value = {
    // 第一个样本没有前值，速率无从计算，不补 0
    down: hasSpeed ? push(trend.value.down, totalSpeed.value.down) : trend.value.down,
    up: hasSpeed ? push(trend.value.up, totalSpeed.value.up) : trend.value.up,
    memory: typeof data.memory === 'number' ? push(trend.value.memory, data.memory) : trend.value.memory
  }
}

function recordClosed(current: MihomoConnection[]) {
  const next = new Map(current.map(connection => [connection.id, connection]))
  const at = Date.now()
  const gone = [...lastById.values()].filter(connection => !next.has(connection.id)).map(connection => toRow(connection, at))
  if (gone.length) closed.value = [...gone, ...closed.value].slice(0, CLOSED_LIMIT)
  lastById = next
}

function schedule() {
  if (timer) clearTimeout(timer)
  timer = null
  if (disposed || !props.active || !live.value) return
  timer = setTimeout(async () => {
    if (document.visibilityState === 'visible') await load()
    schedule()
  }, POLL_MS)
}

async function closeOne(id: string) {
  try {
    await mihomoPanelApi.closeConnection(props.agentId, id)
    if (snapshot.value?.connections) snapshot.value.connections = snapshot.value.connections.filter(c => c.id !== id)
  } catch (failure) {
    notify.error('断开连接失败', panelError(failure))
  }
}

async function closeAll() {
  if (closingAll.value || view.value === 'closed') return
  // 在确认前冻结筛选范围：翻页、轮询或修改筛选均不能扩大将要断开的连接范围。
  const filtered = filtering.value
  const targets = activeMatches.value.map(row => row.id)
  const agentId = props.agentId
  if (!targets.length) return
  const description = filtered ? `将断开当前筛选出的 ${targets.length} 条活动连接（包含其他页）。正在进行的下载和会话会中断。` : '将断开 Mihomo 上的全部活动连接，正在进行的下载和会话会中断。'
  closingAll.value = true
  try {
    if (!await confirmDanger(description, { title: '断开连接', confirmText: '断开' })) return
    if (disposed || props.agentId !== agentId) return
    if (filtered) {
      let failed = 0
      // 大列表限制并发，避免批量操作同时挤满 Agent 的请求队列。
      for (let offset = 0; offset < targets.length; offset += 8) {
        if (disposed || props.agentId !== agentId) return
        const results = await Promise.allSettled(targets.slice(offset, offset + 8).map(id => mihomoPanelApi.closeConnection(agentId, id)))
        failed += results.filter(result => result.status === 'rejected').length
      }
      if (failed) notify.warning(`有 ${failed} 条连接断开失败`)
      else notify.success(`已断开 ${targets.length} 条连接`)
    } else {
      await mihomoPanelApi.closeAllConnections(agentId)
      notify.success('已断开全部连接')
    }
    await load()
  } catch (failure) {
    notify.error('断开连接失败', panelError(failure))
  } finally {
    closingAll.value = false
  }
}

watch(() => [props.active, live.value], ([active, polling]) => {
  // 暂停后再恢复时旧样本已过时，不能拿来算速率
  if (!active || !polling) previous = null
  else load()
  schedule()
})
onMounted(async () => {
  if (props.active) await load()
  schedule()
})
onUnmounted(() => {
  disposed = true
  if (timer) clearTimeout(timer)
  controller?.abort()
})
watch(() => props.agentId, () => { notesEpoch++; notes.value = {}; notesError.value = ''; noteIP.value = ''; ruleAction.value = null; void loadNotes() })
watch(() => props.active, active => { if (active) void loadNotes() })
onMounted(loadNotes)
onUnmounted(() => { notesEpoch++ })
</script>

<style scoped>
.connection-table-frame {
  container-type: inline-size;
}
.connection-table-scroll {
  scrollbar-gutter: stable;
}
.connection-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 0 8px;
  background: var(--card);
  white-space: normal;
}
.connection-table td {
  padding: 8px;
  white-space: normal;
}
.connection-table .connection-actions {
  width: 104px;
}
.connection-sort {
  display: flex;
  width: 100%;
  min-height: 44px;
  align-items: center;
  gap: 4px;
  border-radius: var(--radius-sm);
  color: inherit;
}
.connection-sort:hover {
  color: var(--primary-accent);
}
.connection-sort:focus-visible {
  outline: 2px solid var(--ring);
}
.connection-wrap {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
.connection-table-scroll:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}
.connection-filter-fields :deep([role='combobox']) {
  min-height: 44px;
}
/* Keep the same rows and actions on narrow panels, without horizontal scrolling. */
@container (max-width: 740px) {
  .connection-table,
  .connection-table tbody {
    display: block;
  }
  .connection-table thead {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .connection-table tbody tr {
    position: relative;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px 16px;
    padding: 12px;
    border-bottom: 1px solid var(--border);
  }
  .connection-table tbody tr:last-child {
    border-bottom: 0;
  }
  .connection-table td {
    display: block;
    min-width: 0;
    padding: 0;
    border-bottom: 0;
    text-align: left;
  }
  .connection-table td[data-column]::before {
    content: attr(data-label);
    display: block;
    margin-bottom: 4px;
    color: var(--muted-foreground);
    font-family: var(--font-sans);
    font-size: 11px;
  }
  .connection-table td[data-column='target'] {
    grid-column: 1 / -1;
    padding-right: 96px;
  }
  .connection-table td[data-column='target']::before {
    display: none;
  }
  .connection-table td.connection-actions {
    position: absolute;
    top: 12px;
    right: 8px;
    width: 88px;
  }
  .connection-transfer {
    justify-content: flex-start;
  }
}
</style>
