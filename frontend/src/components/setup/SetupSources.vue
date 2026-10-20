<script setup lang="ts">
import { ref } from 'vue'
import { LoaderCircle } from '@lucide/vue'
import { subscriptionApi } from '@/api'
import { setupApi, type SetupIndex, type SetupSession } from '@/api/setup'
import { setupError } from '@/lib/setupFlow'
import FormField from '@/components/common/FormField.vue'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import Segmented from '@/components/common/Segmented.vue'

const props = defineProps<{ session: SetupSession; options: SetupIndex }>()
const emit = defineEmits<{ saved: [value: SetupSession]; busy: [value: boolean] }>()
const mode = ref(props.session.subscription_ids.length || props.session.node_ids.length ? 'existing' : 'url')
const url = ref('')
const subscriptions = ref([...props.session.subscription_ids])
const nodes = ref([...props.session.node_ids])
const busy = ref(false)
const error = ref('')
const result = ref('')
const sourceOptions = [{ label: '订阅链接', value: 'url' }, { label: '已有资源', value: 'existing' }]
function toggle(list: string[], id: string) { const index = list.indexOf(id); if (index < 0) list.push(id); else list.splice(index, 1) }
async function save() {
  if (busy.value) return
  busy.value = true; emit('busy', true); error.value = ''; result.value = ''
  try {
    const { data: session } = await setupApi.source(props.session.id, mode.value === 'url'
      ? { url: url.value } : { subscription_ids: subscriptions.value, node_ids: nodes.value })
    let count = session.node_ids.length
    for (const id of session.subscription_ids) {
      const { data } = await subscriptionApi.fetch(id, true)
      if (!data.success || !data.count) throw new Error('订阅未返回可用节点，请检查链接或筛选条件')
      if (data.from_cache && data.fetch_error) throw new Error('本次订阅读取失败，当前只有旧缓存。请重试或选择其他来源')
      count += data.count
    }
    result.value = `已读取 ${count} 个节点`
    emit('saved', session)
  } catch (failure) { error.value = setupError(failure) }
  finally { busy.value = false; emit('busy', false) }
}
</script>

<template>
  <div class="space-y-5">
    <p v-if="session.prepared" class="m-0 text-sm text-muted-foreground">该方案的来源已保存。后续调整可前往订阅来源或节点库。</p>
    <template v-else>
      <Segmented v-model="mode" :options="sourceOptions" label="节点来源方式" />
      <FormField v-if="mode === 'url'" label="订阅链接" html-for="setup-subscription" help="粘贴服务商提供的 HTTP(S) 订阅链接。读取使用现有订阅解析服务；失败后可修改链接重试。链接可能含访问凭据，请勿分享。">
        <Input id="setup-subscription" v-model="url" type="url" autocomplete="off" spellcheck="false" placeholder="https://example.com/subscribe…" :disabled="busy" @keydown.enter="save" />
      </FormField>
      <div v-else class="space-y-4">
        <FormField label="已有订阅" help="选中的订阅会重新读取节点，保留现有订阅和其他配置空间的设置。">
          <p v-if="!options.subscriptions.length" class="m-0 text-sm text-muted-foreground">还没有订阅，可以切换到订阅链接添加。</p>
          <label v-for="item in options.subscriptions" :key="item.id" class="flex min-h-11 items-center gap-2 border-b border-border/50 py-2 text-sm">
            <Checkbox :model-value="subscriptions.includes(item.id)" :disabled="busy" @update:model-value="toggle(subscriptions, item.id)" />{{ item.name }}
          </label>
        </FormField>
        <FormField v-if="options.nodes.length" label="已有节点" help="可以同时选择订阅和手动节点。向导只添加引用，不复制或修改节点凭据。">
          <div class="max-h-60 overflow-y-auto">
            <label v-for="item in options.nodes" :key="item.id" class="flex min-h-11 items-center gap-2 border-b border-border/50 py-2 text-sm">
              <Checkbox :model-value="nodes.includes(item.id)" :disabled="busy" @update:model-value="toggle(nodes, item.id)" />{{ item.name }}
            </label>
          </div>
        </FormField>
      </div>
      <p v-if="error" role="alert" class="m-0 text-sm text-destructive-accent">{{ error }}</p>
      <p v-if="result" role="status" class="m-0 text-sm text-success-accent">{{ result }}</p>
      <Button :disabled="busy || (mode === 'url' ? !url.trim() : !subscriptions.length && !nodes.length)" class="min-h-11" @click="save"><LoaderCircle v-if="busy" class="size-4 animate-spin" />{{ busy ? '正在读取节点…' : '读取并继续' }}</Button>
    </template>
  </div>
</template>
