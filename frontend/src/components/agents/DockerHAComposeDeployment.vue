<script setup lang="ts">
import { Copy, Download } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { notify } from '@/lib/feedback'
import { copyText } from '@/lib/clipboard'
import DockerHAGuideCommand from './DockerHAGuideCommand.vue'
import type { DockerHAComposeDeployment } from '@/types/dockerHAGuide'

const props = defineProps<{ deployment: DockerHAComposeDeployment; compose: string }>()
function download() {
  try {
    const url = URL.createObjectURL(new Blob([props.compose], { type: 'text/yaml;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = props.deployment.filename
    document.body.appendChild(link)
    try { link.click() }
    finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  } catch { notify.error('下载失败，请重新生成 Compose 后重试。') }
}
async function copy() {
  try { await copyText(props.compose); notify.success('Compose 已复制，可粘贴到容器管理器的项目中') }
  catch { notify.error('复制失败，请下载文件或展开 YAML 手动复制。') }
}
</script>

<template>
  <div class="flex min-w-0 flex-col gap-4" data-compose-deployment>
    <section class="flex min-w-0 flex-col gap-2" aria-label="1. 保存 Compose">
      <p class="m-0 text-[13px] font-medium">1. 保存 Compose</p>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">只需这一份 compose.yaml。下载到 Docker 主机，或复制到群晖等容器管理器的项目中。</p>
      <div class="flex flex-wrap gap-2">
        <Button variant="outline" class="min-h-11" @click="download"><Download class="size-4" />下载 compose.yaml</Button>
        <Button variant="ghost" class="min-h-11" @click="copy"><Copy class="size-4" />复制 Compose</Button>
      </div>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">包含限时安装凭据，首次安装请在 24 小时内完成，请勿分享。</p>
    </section>
    <DockerHAGuideCommand title="2. 启动项目" :command="deployment.command" note="在 compose.yaml 所在目录执行；使用容器管理器时直接启动项目，无需 SSH 初始化。" />
    <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">目录和登记由容器自动准备。项目名：<code class="font-mono [overflow-wrap:anywhere]">{{ deployment.project_name }}</code>。上线后前往下一步选择两台节点；支持 Shell 与 Docker 混合配对。</p>
    <p v-if="deployment.identity_mode === 'installation'" class="m-0 text-[12px] leading-relaxed text-muted-foreground">{{ deployment.service_count === 2 ? 'Host 运行 OxiDNS 和独立 VIP 服务。' : 'macvlan 容器内包含 OxiDNS、Agent 和 keepalived。' }}身份和配置保存在数据目录，重建时请保留；无需单独初始化。</p>
    <p v-if="deployment.identity_mode === 'docker-volume'" class="m-0 text-[12px] leading-relaxed text-muted-foreground">本机标识保存在共享 Docker 卷 configflow-ha-host-identity 中；请保留该卷，不要复制到另一台主机。</p>
  </div>
</template>
