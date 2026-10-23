<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowRight } from '@lucide/vue'
import { setupApi, type SetupSession } from '@/api/setup'
import { abandonSetup, setupError } from '@/lib/setupFlow'
import { notify } from '@/lib/feedback'
import { useProfileStore } from '@/stores/profile'
import { Button } from '@/components/ui/button'
import FieldHelp from '@/components/common/FieldHelp.vue'

const visible = ref(false)
const abandoning = ref(false)
const session = ref<SetupSession>()
const profileStore = useProfileStore()
const emit = defineEmits<{ abandoned: [] }>()
onMounted(async () => {
  try {
    const { data } = await setupApi.index()
    session.value = data.sessions.find(item => !item.completed && !item.abandoned && ['router', 'gateway'].includes(item.goal))
    visible.value = data.suggested || !!session.value
  } catch { /* This optional entry must not hide the dashboard on a read failure. */ }
})
async function abandon() {
  if (!session.value || abandoning.value) return
  abandoning.value = true
  try {
    const result = await abandonSetup(session.value.id)
    if (!result) return
    visible.value = false
    if (result.delete_profile) await profileStore.refreshProfiles()
    emit('abandoned')
  } catch (failure) { notify.error(setupError(failure)) }
  finally { abandoning.value = false }
}
</script>

<template>
  <section v-if="visible" class="mb-5 flex flex-wrap items-center gap-3 rounded-xl border border-primary-accent/30 bg-primary-soft px-5 py-4 max-sm:mb-3 max-sm:px-3 max-sm:py-3" aria-label="首次使用向导">
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-1">
        <h2 class="m-0 text-sm font-semibold">{{ session ? `继续配置 · ${session.name}` : '从这里开始配置' }}</h2>
        <FieldHelp label="首次使用向导" text="配置 Mihomo + OxiDNS 分流，或使用 Linux 网关。按步骤读取订阅、准备配置、安装并检查两项核心，再配置主路由或客户端。向导使用独立配置空间，刷新后可继续，也可以从系统设置重新进入。" />
      </div>
    </div>
    <Button v-if="session" variant="ghost" size="sm" class="min-h-11" :disabled="abandoning" @click="abandon">放弃</Button>
    <Button as-child size="sm" class="min-h-11">
      <RouterLink :to="session ? { path: '/setup', query: { session: session.id } } : '/setup'">{{ session ? '继续向导' : '开始配置' }}<ArrowRight class="size-3.5" /></RouterLink>
    </Button>
  </section>
</template>
