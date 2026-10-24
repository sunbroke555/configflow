<template>
  <div class="min-w-0">
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <Button type="button" variant="outline" role="combobox" aria-label="客户端 IP" :aria-expanded="open"
        class="min-h-11 w-full min-w-0 justify-between gap-2 bg-card font-normal" :disabled="!agentId || !recorder">
        <span :class="['min-w-0 truncate', modelValue ? 'font-mono' : 'text-muted-foreground']" :title="modelValue || placeholder">{{ modelValue ? clientLabel(modelValue) : placeholder }}</span>
        <ChevronsUpDown class="size-4 shrink-0 text-muted-foreground" />
      </Button>
    </PopoverTrigger>
    <PopoverContent align="start" class="w-[340px] max-w-[calc(100vw-2rem)] p-0">
      <Command :model-value="modelValue" selection-behavior="replace">
        <div class="flex items-center gap-2 border-b px-3 py-1">
          <Search class="size-4 shrink-0 text-muted-foreground" />
          <ListboxFilter :model-value="search" @update:model-value="changeSearch"
            class="min-h-11 w-full min-w-0 rounded-sm border-0 bg-transparent px-0 font-mono text-base text-foreground outline-none placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-primary-accent md:text-sm"
            aria-label="搜索已有客户端 IP" placeholder="搜索 IP 或设备备注…" />
        </div>
        <div v-if="allowAll" class="border-b px-1 py-1">
          <Button type="button" variant="ghost" class="min-h-11 w-full justify-start font-normal" @click="select('')">
            <Check :class="['size-4', modelValue ? 'opacity-0' : 'text-primary-accent']" />全部客户端
          </Button>
        </div>
        <p v-if="loading" role="status" class="m-0 flex items-center gap-2 px-3 py-3 text-xs text-muted-foreground"><Loader2 class="size-4 animate-spin" />正在读取客户端…</p>
        <div v-else-if="error" class="px-3 py-2">
          <p role="alert" class="m-0 text-xs text-warning-accent">{{ error }}</p>
          <Button type="button" variant="ghost" class="mt-1 min-h-11" @click="load">重试</Button>
        </div>
        <CommandList aria-label="已有客户端 IP" class="max-h-64">
          <CommandGroup>
          <CommandItem v-for="client in options" :key="client.ip" :value="client.ip" class="min-h-11 gap-2" @select="select(client.ip)">
            <Check :class="['size-4 shrink-0', modelValue === client.ip ? 'text-primary-accent' : 'opacity-0']" />
            <span class="min-w-0 break-all text-xs"><span v-if="note(client.ip)" class="mb-0.5 block">{{ note(client.ip) }}</span><span class="font-mono text-muted-foreground">{{ client.ip }}</span></span>
            <span v-if="client.ip === modelValue" class="ml-auto shrink-0 text-xs text-muted-foreground">已选</span>
          </CommandItem>
          </CommandGroup>
        </CommandList>
        <p v-if="!loading && !searchPending && !error && !options.length" class="m-0 px-3 py-4 text-xs text-muted-foreground">{{ search ? '没有找到匹配的客户端 IP。' : '此时间范围内暂无客户端记录。' }}</p>
        <p v-if="limited && !error" class="m-0 border-t px-3 py-2.5 text-xs text-muted-foreground">已列出最近 10,000 条匹配记录中的客户端，可搜索其他 IP。</p>
      </Command>
    </PopoverContent>
  </Popover>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { oxiDNSQueryTimeRange, useOxiDNSClock } from '@/composables/useOxiDNSClock'
import { Check, ChevronsUpDown, Loader2, Search } from '@lucide/vue'
import { ListboxFilter } from 'reka-ui'
import { deviceNote } from '@/lib/device-notes'
import { oxidnsMonitorApi } from '@/api'
import type { OxiDNSClients, OxiDNSQueryFilters } from '@/types/oxidnsMonitor'
import { Button } from '@/components/ui/button'
import { Command, CommandGroup, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const props = withDefaults(defineProps<{ modelValue: string; agentId: string; recorder: string; range: OxiDNSQueryFilters['range']; collectedAt?: string; allowAll?: boolean; placeholder?: string; notes?: Record<string, string> }>(), { allowAll: true, placeholder: '全部客户端' })
const clock = useOxiDNSClock(computed(() => props.collectedAt))
const note = (ip: string) => deviceNote(props.notes || {}, ip)
const clientLabel = (ip: string) => note(ip) ? `${note(ip)} · ${ip}` : ip
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const open = ref(false)
const search = ref('')
const clients = ref<OxiDNSClients['clients']>([])
const loading = ref(false)
const searchPending = ref(false)
const error = ref('')
const limited = ref(false)
let controller: AbortController | undefined
let debounce: ReturnType<typeof setTimeout> | undefined
let epoch = 0
let disposed = false

const options = computed(() => {
  const values = new Map(clients.value.map(client => [client.ip, client]))
  if (props.modelValue && !values.has(props.modelValue)) values.set(props.modelValue, { ip: props.modelValue, count: 0 })
  return [...values.values()].filter(client => clientLabel(client.ip).toLowerCase().includes(search.value.toLowerCase().trim()))
})

function cancel() {
  epoch++
  controller?.abort()
  if (debounce) clearTimeout(debounce)
  debounce = undefined
  loading.value = false
  searchPending.value = false
}
async function load() {
  cancel()
  if (disposed || !open.value || !props.agentId || !props.recorder) return
  const requestEpoch = epoch
  const current = controller = new AbortController()
  loading.value = true
  error.value = ''
  const keyword = search.value.trim()
  // The core searches IPs only; names are matched against the bounded result here.
  const matchesNote = !!keyword && Object.values(props.notes || {}).some(name => name.toLowerCase().includes(keyword.toLowerCase()))
  try {
    const { data } = await oxidnsMonitorApi.clients(props.agentId, {
      recorder: props.recorder, search: matchesNote ? undefined : keyword || undefined,
      ...oxiDNSQueryTimeRange(props.range, clock.now())
    }, { signal: current.signal })
    if (disposed || current.signal.aborted || requestEpoch !== epoch) return
    if (!data?.success || !Array.isArray(data.clients)) throw new Error(data?.message || '客户端列表格式异常，请重试。')
    clients.value = data.clients
    limited.value = data.sample_limited === true
  } catch (failure: any) {
    if (!disposed && !current.signal.aborted && requestEpoch === epoch) error.value = failure?.response?.data?.message || failure?.message || '暂时无法读取客户端列表。'
  } finally {
    if (requestEpoch === epoch) loading.value = false
  }
}
function changeSearch(value: string) {
  search.value = value
  cancel()
  error.value = ''
  limited.value = false
  searchPending.value = true
  debounce = setTimeout(() => { void load() }, 300)
}
function select(value: string) {
  emit('update:modelValue', value)
  open.value = false
}
watch(open, value => {
  cancel()
  if (value) {
    search.value = ''
    void load()
  }
})
watch(() => [props.agentId, props.recorder, props.range], () => {
  cancel()
  clients.value = []
  error.value = ''
  limited.value = false
  open.value = false
  search.value = ''
})
onUnmounted(() => { disposed = true; cancel() })
</script>
