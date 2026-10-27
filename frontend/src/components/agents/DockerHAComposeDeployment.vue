<script setup lang="ts">
import { Download } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { notify } from '@/lib/feedback'
import DockerHAGuideCommand from './DockerHAGuideCommand.vue'
import type { DockerHAComposeBundle } from '@/types/dockerHAGuide'

const props = defineProps<{ bundle: DockerHAComposeBundle }>()
function download() {
  try {
    const bytes = Uint8Array.from(atob(props.bundle.content_base64), char => char.charCodeAt(0))
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }))
    const link = document.createElement('a')
    link.href = url
    link.download = props.bundle.filename
    document.body.appendChild(link)
    try { link.click() }
    finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  } catch { notify.error('下载失败，请重新生成部署包后重试。') }
}
</script>

<template>
  <div class="flex min-w-0 flex-col gap-4" data-compose-deployment>
    <section class="flex min-w-0 flex-col gap-2" aria-label="1. 下载并解压">
      <p class="m-0 text-[13px] font-medium">1. 下载并解压</p>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">把部署包解压到这台主机上一步填写的数据目录，保留全部文件。NAS 可通过共享文件夹上传。</p>
      <Button variant="outline" class="min-h-11 self-start" @click="download"><Download class="size-4" />下载 Compose 部署包</Button>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">包含本机安装凭据，首次安装请在 24 小时内完成，请勿分享。</p>
    </section>
    <DockerHAGuideCommand title="2. 初始化并启动" :command="`${bundle.prepare_command}\n${bundle.start_command}`" note="在这台主机的 SSH 终端进入解压目录，再执行下面两条命令。初始化会准备数据目录和身份文件。" />
    <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">启动后前往下一步刷新 Agent。群晖等 NAS 也可在初始化成功后，将同一目录中的 compose.yaml 导入容器管理器，项目名填写 <code class="font-mono [overflow-wrap:anywhere]">{{ bundle.project_name }}</code>。</p>
  </div>
</template>
