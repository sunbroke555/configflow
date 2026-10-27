<script setup lang="ts">
import { computed, ref } from 'vue'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import FormField from '@/components/common/FormField.vue'
import DockerHAGuideCommand from './DockerHAGuideCommand.vue'

const systems = [
  { value: 'debian', label: 'Debian / Ubuntu', command: 'apt-get install keepalived iproute2' },
  { value: 'fedora', label: 'Fedora / RHEL', command: 'dnf install keepalived iproute' },
  { value: 'arch', label: 'Arch Linux', command: 'pacman -S --needed keepalived iproute2' },
  { value: 'alpine', label: 'Alpine Linux', command: 'apk add keepalived iproute2' },
]
const system = ref('debian')
const selected = computed(() => systems.find(item => item.value === system.value) || systems[0]!)
</script>

<template>
  <section class="flex min-w-0 flex-col gap-3 text-[13px] leading-relaxed" aria-label="Shell 高可用安装准备">
    <p class="m-0">已有 OxiDNS Agent 可直接前往「验证与配对」。新装时在下方填写这一台；未装核心请勾选「同时安装 OxiDNS 核心」。</p>
    <p class="m-0">两台 Shell 请分别生成各自的安装命令。Docker 端从上方选择「Docker 容器 → OxiDNS → 高可用安装」。</p>
    <p class="m-0">使用两台独立、同网段的 Linux 主机。Shell 主机需使用 systemd / OpenRC，Agent 以 root 运行，并预先安装 keepalived 和 ip 工具。</p>
    <details class="min-w-0">
      <summary class="min-h-11 cursor-pointer py-3 font-medium">安装高可用依赖</summary>
      <div class="flex min-w-0 flex-col gap-3 pt-2">
        <FormField label="Shell 主机系统" html-for="shell-ha-system">
          <Select v-model="system">
            <SelectTrigger id="shell-ha-system" class="min-h-11"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem v-for="item in systems" :key="item.value" :value="item.value">{{ item.label }}</SelectItem></SelectContent>
          </Select>
        </FormField>
        <DockerHAGuideCommand :title="`${selected.label} 依赖安装命令`" :command="selected.command" note="以 root 在每台 Shell 主机执行；已有依赖可跳过。VIP 在下一步统一配置。" />
      </div>
    </details>
    <p class="m-0 text-[12px] text-muted-foreground">新登记设备在「验证与配对」中绑定本配置空间，再开启并检查高可用。</p>
  </section>
</template>
