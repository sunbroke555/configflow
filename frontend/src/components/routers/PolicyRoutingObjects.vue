<template>
  <div class="min-w-0">
    <div class="flex flex-wrap items-center justify-between gap-2 px-5 pb-3 max-md:px-4 max-sm:px-3">
      <span class="text-xs text-muted-foreground">{{ collectedAt ? `读取于 ${collectedAt}` : '' }}</span>
      <div class="cf-section-actions flex flex-wrap gap-2">
        <Button variant="outline" class="min-h-11" :disabled="disabled || !sourcesEditable" @click="emit('add-source')"><Plus class="size-4" />新增来源</Button>
        <Button variant="outline" class="min-h-11" :disabled="disabled" @click="emit('edit-destinations')">编辑目的规则集</Button>
        <Button variant="outline" class="min-h-11" :disabled="loading || disabled" @click="load"><RefreshCw class="size-4" />刷新对象</Button>
      </div>
    </div>
    <p v-if="error" role="alert" class="px-5 text-sm text-destructive-accent">{{ error }}</p>
    <LoadingRows v-if="loading" :rows="4" />
    <template v-else-if="data">
        <p class="m-0 px-5 pb-3 text-xs text-muted-foreground max-md:px-4 max-sm:px-3">编辑会打开对应来源或规则集，修改后需保存并预览发布。下表仍显示设备上的已下发对象；不会直接修改 ROS 字段。</p>
        <template v-if="data.objects.length">
          <div class="flex flex-wrap gap-2 border-t border-border px-5 py-3 max-md:px-4 max-sm:px-3" role="group" aria-label="策略对象类型">
            <Button v-for="group in groups" :key="group.key" size="sm" class="min-h-11" :variant="kind === group.key ? 'secondary' : 'ghost'" :aria-pressed="kind === group.key" @click="kind = group.key">
              {{ group.label }} <span class="num">{{ data.objects.filter(item => item.kind === group.key).length }}</span>
            </Button>
          </div>
          <div class="px-5 pb-3 max-md:px-4 max-sm:px-3"><Input v-model="search" class="min-h-11 max-w-md" aria-label="搜索策略对象" placeholder="搜索名称、地址或动作…" /></div>
          <div v-if="filtered.length" class="cf-table-wrap">
            <table class="cf-table min-w-[720px]">
              <thead><tr><th>名称 / 链</th><th>地址 / 动作</th><th>状态</th><th>实际配置</th><th class="text-right">操作</th></tr></thead>
              <tbody>
                <tr v-for="item in pageRows" :key="item.kind + item.id">
                  <td class="font-mono text-xs">{{ item.fields.list || item.fields.name || item.fields.chain || item.fields['routing-table'] || '—' }}</td>
                  <td class="font-mono text-xs">{{ item.fields.address || item.fields.action || item.fields['dst-address'] || (item.fields.fib === 'true' || item.fields.fib === true ? 'FIB' : '—') }}</td>
                  <td>
                    <span :class="['chip', item.status === 'matched' ? 'chip-ok' : 'chip-warn']">{{ states[item.status] }}</span>
                    <p v-if="item.fields.disabled === 'true' || item.fields.disabled === true" class="mb-0 mt-1 text-xs text-muted-foreground">已停用</p>
                    <p v-if="item.kind === 'routes' && item.status !== 'missing'" class="mb-0 mt-1 text-xs text-muted-foreground">{{ item.fields.active === 'true' || item.fields.active === true ? '路由活跃' : '路由未活跃' }}</p>
                  </td>
                  <td class="min-w-64 max-w-md whitespace-normal break-words">
                    <p v-if="item.fields.gateway" class="m-0 font-mono text-xs">下一跳 {{ item.fields.gateway }}</p>
                    <p v-if="item.fields['new-routing-mark'] || item.fields['jump-target']" class="m-0 font-mono text-xs">→ {{ item.fields['new-routing-mark'] || item.fields['jump-target'] }}</p>
                    <details>
                      <summary class="min-h-11 cursor-pointer content-center text-xs focus-visible:outline-2 focus-visible:outline-primary-accent">{{ item.status === 'missing' ? '查看上次发布记录' : '查看字段' }} · {{ item.id }}</summary>
                      <dl class="m-0 space-y-1 pb-2 font-mono text-xs"><div v-for="(value, field) in item.fields" :key="field"><dt class="inline text-muted-foreground">{{ field }}: </dt><dd class="m-0 inline">{{ value }}</dd></div></dl>
                    </details>
                  </td>
                  <td class="text-right"><Button variant="ghost" class="min-h-11" :disabled="disabled || (item.editor === 'source' && !sourcesEditable)" @click="emit('edit', item)"><Pencil class="size-4" />{{ item.editor === 'source' ? '编辑来源' : item.editor === 'destination' ? '编辑规则集' : '编辑设置' }}</Button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <EmptyState v-else :icon="ListFilter" title="没有匹配的对象" description="此分类下没有记录，或没有符合搜索条件的对象。" />
          <div v-if="filtered.length" class="flex flex-wrap items-center justify-end gap-2 px-5 py-3 text-xs max-md:px-4 max-sm:px-3">
            <span class="num text-muted-foreground">共 {{ filtered.length }} 项 · 第 {{ page }} / {{ pages }} 页</span>
            <Button size="sm" variant="outline" class="min-h-11" :disabled="page === 1" @click="page--">上一页</Button>
            <Button size="sm" variant="outline" class="min-h-11" :disabled="page >= pages" @click="page++">下一页</Button>
          </div>
        </template>
        <EmptyState v-else :icon="Network" title="尚无已下发的策略对象" description="选择规则集和网关，并配置来源设备后，预览发布到 ROS。这里只展示本配置空间的发布记录，不接管手工同名对象。" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ListFilter, Network, Pencil, Plus, RefreshCw } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import { routerApi, type PolicyRoutingObject, type PolicyRoutingView } from '@/api'

const props = withDefaults(defineProps<{ id: string; profile: string; disabled?: boolean; sourcesEditable?: boolean }>(), { sourcesEditable: true })
const emit = defineEmits<{ 'add-source': []; 'edit-destinations': []; edit: [item: PolicyRoutingObject] }>()
const data = ref<PolicyRoutingView | null>(null)
const loading = ref(false)
const error = ref('')
const collectedAt = ref('')
const kind = ref<PolicyRoutingObject['kind']>('addresses')
const search = ref('')
const page = ref(1)
const groups = [{ key: 'addresses', label: '地址列表' }, { key: 'mangle', label: 'Mangle' },
  { key: 'tables', label: '路由表' }, { key: 'routes', label: '下一跳' }, { key: 'nat', label: 'NAT 免转换' }] as const
const states = { matched: '字段与发布记录一致', changed: '已被外部修改', missing: '设备上缺失' }
const filtered = computed(() => (data.value?.objects || []).filter(item => item.kind === kind.value
  && Object.values(item.fields).join(' ').toLowerCase().includes(search.value.trim().toLowerCase())))
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / 50)))
const pageRows = computed(() => filtered.value.slice((page.value - 1) * 50, page.value * 50))
watch([kind, search], () => { page.value = 1 })
let epoch = 0
async function load() {
  const current = ++epoch
  loading.value = true
  error.value = ''
  data.value = null
  collectedAt.value = ''
  page.value = 1
  try {
    const response = await routerApi.policy(props.id, props.profile)
    if (current !== epoch) return
    if (!response.data.success) throw new Error(response.data.message || '读取策略路由失败')
    data.value = response.data.policy
    collectedAt.value = new Date(response.data.generated_at).toLocaleString()
  } catch (failure: any) {
    if (current === epoch) error.value = failure?.response?.data?.message || failure?.message || '读取策略路由失败，请刷新重试。'
  } finally {
    if (current === epoch) loading.value = false
  }
}
watch(() => [props.id, props.profile], () => { kind.value = 'addresses'; search.value = ''; void load() }, { immediate: true })
onBeforeUnmount(() => { epoch++ })
</script>
