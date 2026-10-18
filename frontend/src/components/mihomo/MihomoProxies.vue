<template>
  <div class="min-w-0">
    <Toolbar v-model:search="search" placeholder="搜索策略组或节点">
      <template #search-actions>
        <Button variant="outline" size="icon" class="size-11 shrink-0" aria-label="策略显示设置" title="设置" @click="settingsOpen = true"><Settings2 class="size-4" /></Button>
      </template>
      <template #actions>
        <Button variant="outline" class="min-h-11" :disabled="!groups.length" @click="toggleAll">
          <ChevronsUpDown class="size-3.5" />{{ allExpanded ? '全部收起' : '全部展开' }}
        </Button>
        <Button variant="outline" class="min-h-11" :disabled="!allTestGroups.length || testing.size > 0 || nodeTesting.size > 0 || batchRunning" @click="testAll">
          <Loader2 v-if="batchRunning" class="size-3.5 animate-spin" /><Zap v-else class="size-3.5" />全部分组测速
        </Button>
      </template>
    </Toolbar>
    <div v-if="batchRunning" role="status" class="mb-3 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
      <span>已测 {{ batchDone }} / {{ batchTotal }} 组 · 依次测试，包含隐藏分组</span>
      <Button variant="ghost" size="sm" class="min-h-11" :disabled="batchStop" @click="batchStop = true">{{ batchStop ? '当前分组结束后停止' : '停止后续测速' }}</Button>
    </div>
    <MihomoProxySettings v-model:open="settingsOpen" :settings="settings" @update:settings="applySettings" />

    <LoadingRows v-if="loading && !groups.length" :rows="5" />
    <SectionCard v-else-if="error && !groups.length">
      <EmptyState :icon="TriangleAlert" title="读取策略组失败" :description="error">
        <Button variant="outline" class="min-h-11" @click="load">重试</Button>
      </EmptyState>
    </SectionCard>
    <SectionCard v-else-if="!visibleGroups.length">
      <EmptyState :icon="Search" :title="search ? '没有匹配的策略组' : '暂无策略组'" :description="search ? '换个关键词试试。' : 'Mihomo 配置中没有可展示的策略组。'" />
    </SectionCard>

    <div v-else class="grid min-w-0 gap-3 xl:grid-cols-2" :class="{ 'select-none': settings.disableTextSelection }">
      <section v-for="group in visibleGroups" :key="group.name" class="min-w-0 rounded-xl border border-border bg-card shadow-surface">
        <header class="flex min-w-0 items-center gap-2 p-2 pl-4">
          <button type="button" class="flex min-h-11 min-w-0 flex-1 items-center gap-3 rounded-md text-left focus-visible:outline-2 focus-visible:outline-ring"
            :aria-expanded="isExpanded(group.name)" @click="toggle(group.name)">
            <ChevronRight class="size-4 shrink-0 text-muted-foreground transition-transform duration-200" :class="{ 'rotate-90': isExpanded(group.name) }" aria-hidden="true" />
            <span class="min-w-0 flex-1">
              <span class="flex min-w-0 flex-wrap items-center gap-2">
                <span class="break-all font-semibold text-foreground">{{ group.name }}</span>
                <span class="chip">{{ GROUP_TYPE_LABELS[group.type] || group.type }}</span>
                <span v-if="group.fixed" class="chip chip-warn">已固定</span>
                <span v-if="group.hidden" class="chip">隐藏</span>
                <span class="num text-xs text-muted-foreground">{{ group.all?.length || 0 }} 个</span>
              </span>
              <span v-if="group.now" class="mt-0.5 flex min-w-0 items-center gap-1.5 text-[12.5px] text-muted-foreground">
                <ArrowRight class="size-3 shrink-0" aria-hidden="true" />
                <span class="truncate">{{ group.now }}<template v-if="settings.showFinalNode && finalNode(proxies, group.now) !== group.now"> → {{ finalNode(proxies, group.now) }}</template></span>
                <span class="num shrink-0" :class="toneClass(delayOf(group.now, group))">{{ delayText(delayOf(group.now, group)) }}</span>
              </span>
            </span>
          </button>
          <Button v-if="group.fixed" variant="ghost" size="sm" class="min-h-11" :disabled="busy.has(group.name)" @click="unfix(group)">取消固定</Button>
          <Button variant="ghost" size="icon" class="size-11 shrink-0" :aria-label="`测速 ${group.name}`" :title="'测速'"
            :disabled="batchRunning || testing.has(group.name) || nodeTesting.size > 0" @click="testGroup(group)">
            <Loader2 v-if="testing.has(group.name)" class="size-4 animate-spin" />
            <Zap v-else class="size-4" />
          </Button>
        </header>
        <div v-if="isExpanded(group.name)" class="space-y-3 border-t border-border p-3">
          <div v-for="section in sectionsOf(group)" :key="section.title">
            <div v-if="settings.groupByProvider" class="mb-1.5 text-[11.5px] text-muted-foreground">{{ section.title }} · {{ section.members.length }}</div>
            <div class="grid gap-2" :style="{ gridTemplateColumns: `repeat(auto-fill, minmax(min(${settings.cardMinWidth}px, 100%), 1fr))` }">
              <div v-for="member in section.members" :key="member" class="flex min-w-0 items-center rounded-lg border transition-colors duration-200"
                :class="member === group.now ? 'border-primary-accent/60 bg-primary-soft' : 'border-border bg-background hover:border-border-strong'">
                <button type="button" class="flex min-h-11 min-w-0 flex-1 flex-col justify-center rounded-lg px-3 py-1.5 text-left focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default enabled:hover:bg-accent"
                  :disabled="!canSwitch(group) || busy.has(group.name)" :aria-pressed="member === group.now" @click="select(group, member)">
                  <span class="max-w-full truncate text-[13px] text-foreground" :title="member">{{ member }}</span>
                  <span class="flex max-w-full flex-wrap items-center gap-x-2 text-[11.5px] text-muted-foreground">
                    <span class="truncate" :title="proxies[member]?.type">{{ proxies[member]?.type || '—' }}</span>
                    <span v-if="!canTestNode(member)" class="num whitespace-nowrap" :class="toneClass(delayOf(member, group))">{{ delayText(delayOf(member, group)) }}</span>
                  </span>
                </button>
                <Button v-if="canTestNode(member)" variant="ghost" size="icon" class="h-auto min-h-11 min-w-11 w-auto shrink-0 flex-col gap-0.5 self-stretch px-1.5" :aria-label="`单节点测速 ${member}（${group.name}）`"
                  :disabled="batchRunning || testing.size > 0 || nodeTesting.has(delayKey(member, group))" @click="testNode(member, group)">
                  <Loader2 v-if="nodeTesting.has(delayKey(member, group))" class="size-3.5 animate-spin" /><Zap v-else class="size-3.5" />
                  <span class="num whitespace-nowrap text-[11px] font-normal" :class="toneClass(delayOf(member, group))">{{ delayText(delayOf(member, group)) }}</span>
                </Button>
              </div>
            </div>
          </div>
          <p v-if="!sectionsOf(group).length" class="m-0 text-xs text-muted-foreground">没有可显示的节点{{ settings.hideUnavailable ? '（已隐藏不可用节点）' : '' }}。</p>
        </div>
      </section>
    </div>

    <SectionCard v-if="providers.length" class="mt-5" title="代理集合" description="proxy-providers 订阅，可手动更新或触发健康检查" :padded="false">
      <div class="cf-table-wrap rounded-none border-x-0 border-b-0">
        <table class="cf-table min-w-[640px]">
          <thead><tr><th>名称</th><th>节点数</th><th>流量 / 到期</th><th>更新时间</th><th class="cf-table__right">操作</th></tr></thead>
          <tbody>
            <tr v-for="provider in providers" :key="provider.name">
              <td class="break-all font-medium">{{ provider.name }} <span class="chip ml-1">{{ provider.vehicleType }}</span></td>
              <td class="num">{{ provider.proxies?.length || 0 }}</td>
              <td class="text-xs text-muted-foreground">{{ subscriptionText(provider) }}</td>
              <td class="text-xs text-muted-foreground">{{ provider.updatedAt ? relativeTime(provider.updatedAt) : '—' }}</td>
              <td class="cf-table__right">
                <Button variant="ghost" size="sm" class="min-h-11" :disabled="busy.has('p:' + provider.name)" @click="healthcheck(provider)">健康检查</Button>
                <Button v-if="provider.vehicleType === 'HTTP'" variant="ghost" size="sm" class="min-h-11" :disabled="busy.has('p:' + provider.name)" @click="updateProvider(provider)">
                  <Loader2 v-if="busy.has('p:' + provider.name)" class="size-3.5 animate-spin" />更新
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </SectionCard>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ArrowRight, ChevronRight, ChevronsUpDown, Loader2, Search, Settings2, TriangleAlert, Zap } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import Toolbar from '@/components/common/Toolbar.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import MihomoProxySettings from './MihomoProxySettings.vue'
import { mihomoPanelApi } from '@/api'
import { notify } from '@/lib/feedback'
import { formatBytes, relativeTime } from '@/lib/format'
import {
  GROUP_TYPE_LABELS, GROUP_TYPES, SWITCHABLE_GROUP_TYPES, delayText, delayTone, finalNode, latestDelay, loadProxySettings,
  orderedGroups, panelError, saveProxySettings, type MihomoProvider, type MihomoProxy, type ProxyPanelSettings
} from '@/types/mihomo'

const props = defineProps<{ agentId: string; reloadToken: number }>()

const proxies = ref<Record<string, MihomoProxy>>({})
const providers = ref<MihomoProvider[]>([])
/** 节点名 → 所属代理集合；default 是配置文件里直接写的节点 */
const nodeProvider = ref(new Map<string, string>())
const settings = ref<ProxyPanelSettings>(loadProxySettings())
const settingsOpen = ref(false)
function applySettings(value: ProxyPanelSettings) {
  settings.value = value
  saveProxySettings(value)
}
const loading = ref(false)
const error = ref('')
const search = ref('')
const expanded = ref(new Set<string>())
const testing = reactive(new Set<string>())
const nodeTesting = reactive(new Set<string>())
const batchRunning = ref(false)
const batchStop = ref(false)
const batchDone = ref(0)
const batchTotal = ref(0)
let disposed = false
let testEpoch = 0
const delayKey = (name: string, group?: MihomoProxy) => JSON.stringify([name, group?.testUrl || ''])
const canTestNode = (name: string) => !!proxies.value[name] && !GROUP_TYPES.has(proxies.value[name].type) && !['Reject', 'RejectDrop'].includes(proxies.value[name].type)
const busy = reactive(new Set<string>())
/** 本次会话里测速得到的延迟，优先于核心记录的历史 */
const delays = reactive(new Map<string, number>())

const allTestGroups = computed(() => orderedGroups(proxies.value, true))
const groups = computed(() => orderedGroups(proxies.value, settings.value.showHiddenGroups))
const visibleGroups = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  if (!keyword) return groups.value
  return groups.value.filter(group => group.name.toLowerCase().includes(keyword)
    || group.all?.some(member => member.toLowerCase().includes(keyword)))
})
const allExpanded = computed(() => groups.value.length > 0 && groups.value.every(group => expanded.value.has(group.name)))

const isExpanded = (name: string) => expanded.value.has(name) || (!!search.value.trim() && visibleGroups.value.length <= 6)
function toggle(name: string) {
  const next = new Set(expanded.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  expanded.value = next
}
function toggleAll() {
  expanded.value = allExpanded.value ? new Set() : new Set(groups.value.map(group => group.name))
}

function membersOf(group: MihomoProxy) {
  const keyword = search.value.trim().toLowerCase()
  let members = group.all || []
  if (keyword && !group.name.toLowerCase().includes(keyword)) members = members.filter(member => member.toLowerCase().includes(keyword))
  if (settings.value.hideUnavailable) {
    members = members.filter(member => member === group.now || (delayOf(member, group) !== 0 && proxies.value[member]?.alive !== false))
  }
  if (settings.value.sort === 'name') members = [...members].sort((a, b) => a.localeCompare(b))
  if (settings.value.sort === 'delay') {
    // 未测速和超时排在最后
    const rank = (member: string) => { const delay = delayOf(member, group); return delay && delay > 0 ? delay : Number.MAX_SAFE_INTEGER }
    members = [...members].sort((a, b) => rank(a) - rank(b))
  }
  return members
}

function sectionsOf(group: MihomoProxy): Array<{ title: string; members: string[] }> {
  const members = membersOf(group)
  if (!settings.value.groupByProvider) return members.length ? [{ title: '', members }] : []
  const sections = new Map<string, string[]>()
  for (const member of members) {
    const title = GROUP_TYPES.has(proxies.value[member]?.type || '') ? '策略组'
      : nodeProvider.value.get(member) === 'default' || !nodeProvider.value.has(member) ? '配置节点' : nodeProvider.value.get(member) as string
    sections.set(title, [...(sections.get(title) || []), member])
  }
  return [...sections].map(([title, items]) => ({ title, members: items }))
}

const canSwitch = (group: MihomoProxy) => SWITCHABLE_GROUP_TYPES.has(group.type)

/** 成员是策略组时，延迟取它当前选中的节点 */
function delayOf(name: string, group?: MihomoProxy, depth = 0): number | undefined {
  if (delays.has(delayKey(name, group))) return delays.get(delayKey(name, group))
  const proxy = proxies.value[name]
  if (!proxy) return undefined
  const own = latestDelay(proxy, group?.testUrl)
  if (own !== undefined || !proxy.now || depth > 5) return own
  return delayOf(proxy.now, group, depth + 1)
}

const toneClass = (delay: number | undefined) => ({
  success: 'text-success-accent', warning: 'text-warning-accent', danger: 'text-destructive-accent', muted: 'text-muted-foreground'
})[delayTone(delay)]

function subscriptionText(provider: MihomoProvider) {
  const info = provider.subscriptionInfo
  if (!info?.Total) return '—'
  const used = formatBytes((info.Upload || 0) + (info.Download || 0))
  const expire = info.Expire ? ' · ' + new Date(info.Expire * 1000).toLocaleDateString() + ' 到期' : ''
  return `${used} / ${formatBytes(info.Total)}${expire}`
}

let epoch = 0
async function load() {
  const requestEpoch = ++epoch
  loading.value = true
  error.value = ''
  try {
    const [proxyResult, providerResult] = await Promise.all([
      mihomoPanelApi.proxies(props.agentId),
      mihomoPanelApi.proxyProviders(props.agentId).catch(() => null)
    ])
    if (requestEpoch !== epoch) return
    const providerMap: Record<string, MihomoProvider> = providerResult?.data.data?.providers || {}
    // /proxies 不含 proxy-providers 里的节点，合并进来才能显示策略组成员的类型和延迟
    const providerProxies = Object.fromEntries(Object.values(providerMap)
      .flatMap(provider => provider.proxies || []).map(proxy => [proxy.name, proxy]))
    nodeProvider.value = new Map(Object.values(providerMap)
      .flatMap(provider => (provider.proxies || []).map(proxy => [proxy.name, provider.name] as [string, string])))
    proxies.value = { ...providerProxies, ...(proxyResult.data.data?.proxies || {}) }
    providers.value = Object.values(providerMap).filter(provider => provider.vehicleType !== 'Compatible')
  } catch (failure) {
    if (requestEpoch === epoch) error.value = panelError(failure, '暂时无法读取策略组。')
  } finally {
    if (requestEpoch === epoch) loading.value = false
  }
}

async function select(group: MihomoProxy, member: string) {
  if (member === group.now && !group.fixed) return
  busy.add(group.name)
  try {
    await mihomoPanelApi.selectProxy(props.agentId, group.name, member)
    proxies.value[group.name] = { ...group, now: member, fixed: group.type === 'Selector' ? group.fixed : member }
    const closed = settings.value.closeConnectionsOnSwitch ? await closeGroupConnections(group.name) : 0
    notify.success(`${group.name} 已切换到 ${member}`, closed ? `已断开经过该策略组的 ${closed} 条连接` : undefined)
  } catch (failure) {
    notify.error('切换失败', panelError(failure))
  } finally {
    busy.delete(group.name)
  }
}

/** 断开链路经过该策略组的连接，让后续连接走新节点；失败不影响切换结果 */
async function closeGroupConnections(groupName: string): Promise<number> {
  try {
    const { data } = await mihomoPanelApi.connections(props.agentId)
    const targets = ((data.data?.connections || []) as Array<{ id: string; chains: string[] }>)
      .filter(connection => connection.chains.includes(groupName))
    const results = await Promise.allSettled(targets.map(connection => mihomoPanelApi.closeConnection(props.agentId, connection.id)))
    return results.filter(result => result.status === 'fulfilled').length
  } catch {
    return 0
  }
}

async function unfix(group: MihomoProxy) {
  busy.add(group.name)
  try {
    await mihomoPanelApi.unfixProxy(props.agentId, group.name)
    notify.success(`${group.name} 已恢复自动选择`)
    await load()
  } catch (failure) {
    notify.error('取消固定失败', panelError(failure))
  } finally {
    busy.delete(group.name)
  }
}

async function testNode(name: string, group: MihomoProxy) {
  const key = delayKey(name, group)
  if (batchRunning.value || testing.size || nodeTesting.has(key)) return
  const generation = testEpoch
  const agentId = props.agentId
  nodeTesting.add(key)
  try {
    const { data } = await mihomoPanelApi.proxyDelay(agentId, name, group.testUrl)
    if (disposed || generation !== testEpoch) return
    const delay = data.data?.delay
    if (typeof delay !== 'number' || !Number.isFinite(delay) || delay < 0) throw new Error('测速结果无效，请重试。')
    delays.set(key, delay)
  } catch (failure) {
    if (!disposed && generation === testEpoch) notify.error(`${name} 测速失败`, panelError(failure))
  } finally { if (generation === testEpoch) nodeTesting.delete(key) }
}

async function testGroup(group: MihomoProxy, inBatch = false): Promise<boolean> {
  if (testing.has(group.name) || nodeTesting.size || (batchRunning.value && !inBatch)) return false
  const generation = testEpoch
  const agentId = props.agentId
  testing.add(group.name)
  try {
    const { data } = await mihomoPanelApi.groupDelay(agentId, group.name, group.testUrl)
    if (disposed || generation !== testEpoch) return false
    const result: Record<string, number> = data.data || {}
    for (const member of group.all || []) delays.set(delayKey(member, group), result[member] ?? 0)
    if (!inBatch && group.type !== 'Selector') await load()
    return true
  } catch (failure) {
    if (!disposed && generation === testEpoch) notify.error(`${group.name} 测速失败`, panelError(failure))
    return false
  } finally { if (generation === testEpoch) testing.delete(group.name) }
}

async function testAll() {
  if (batchRunning.value || testing.size || nodeTesting.size) return
  const queue = [...allTestGroups.value]
  if (!queue.length) return
  const generation = testEpoch
  batchRunning.value = true
  batchStop.value = false
  batchDone.value = 0
  batchTotal.value = queue.length
  let failed = 0
  try {
    for (const group of queue) {
      if (disposed || generation !== testEpoch || batchStop.value) break
      const success = await testGroup(group, true)
      if (disposed || generation !== testEpoch) return
      if (!success) failed++
      batchDone.value++
    }
    if (disposed || generation !== testEpoch) return
    notify.info(batchStop.value ? '已停止后续测速' : '全部分组测速完成', `已测 ${batchDone.value} 组${failed ? `，${failed} 组失败` : ''}`)
    await load()
  } finally { if (generation === testEpoch) batchRunning.value = false }
}

async function updateProvider(provider: MihomoProvider) {
  busy.add('p:' + provider.name)
  try {
    await mihomoPanelApi.updateProxyProvider(props.agentId, provider.name)
    notify.success(`${provider.name} 已更新`)
    await load()
  } catch (failure) {
    notify.error('更新代理集合失败', panelError(failure))
  } finally {
    busy.delete('p:' + provider.name)
  }
}

async function healthcheck(provider: MihomoProvider) {
  busy.add('p:' + provider.name)
  try {
    await mihomoPanelApi.healthcheckProxyProvider(props.agentId, provider.name)
    notify.success(`${provider.name} 健康检查完成`)
    delays.clear()
    await load()
  } catch (failure) {
    notify.error('健康检查失败', panelError(failure))
  } finally {
    busy.delete('p:' + provider.name)
  }
}

watch(() => props.reloadToken, () => { delays.clear(); load() })
watch(() => props.agentId, () => {
  testEpoch++; epoch++; testing.clear(); nodeTesting.clear(); delays.clear(); proxies.value = {}; providers.value = []
  batchRunning.value = false; batchStop.value = true; void load()
})
onUnmounted(() => { disposed = true; testEpoch++; epoch++; batchStop.value = true })
onMounted(load)
</script>
