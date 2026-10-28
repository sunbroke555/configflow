<script setup lang="ts">
defineProps<{ existingAgent?: boolean }>()
</script>

<template>
  <section class="flex min-w-0 flex-col gap-3 text-[13px] leading-relaxed" aria-label="Shell 高可用安装准备">
    <p class="m-0">填写这一台的信息，复制一条命令执行；脚本会自动检查系统并补齐 keepalived、ip 等高可用工具。</p>
    <p v-if="existingAgent" class="m-0">已有 Agent 可直接验证与配对。缺少依赖时重新运行自动准备命令；脚本沿用本机已有身份、端口和核心路径，保留现有核心。</p>
    <p v-else class="m-0">默认同时安装 OxiDNS 与 Agent，保留已有程序和配置。首次接管自定义核心时可取消勾选，填写实际路径和服务设置。</p>
    <p class="m-0">两台独立、同网段的 Linux 主机分别执行各自命令；Docker 端从上方切换至 Docker 高可用安装。</p>
    <details class="min-w-0">
      <summary class="min-h-11 cursor-pointer py-3 font-medium">支持的系统与安装范围</summary>
      <p class="mt-0">支持 systemd / OpenRC，自动识别 Debian / Ubuntu、Alpine、Arch、Fedora / RHEL 等系统的包管理器。依赖准备失败时停止安装并显示原因。</p>
      <p class="mb-0">已有 keepalived 配置会保留。新登记设备在「验证与配对」中绑定本配置空间，再设置 VIP 并开启高可用；安装完成不代表已开启。</p>
    </details>
  </section>
</template>
