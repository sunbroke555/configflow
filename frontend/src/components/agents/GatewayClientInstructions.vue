<script setup lang="ts">
defineProps<{ address: string }>()
</script>

<template>
  <div class="flex flex-col gap-3" aria-label="客户端接入检查">
    <h3 class="m-0 text-sm font-semibold">客户端接入检查</h3>
    <p class="m-0 text-sm">转发已就绪。请先在一台测试设备上修改网络设置，本页不会自动修改手机或电脑。</p>
    <dl class="m-0 grid grid-cols-2 gap-3 rounded-lg border border-border p-3 text-sm"><div><dt class="text-xs text-muted-foreground">测试设备网关</dt><dd class="m-0 font-mono">{{ address }}</dd></div><div><dt class="text-xs text-muted-foreground">测试设备 DNS</dt><dd class="m-0 font-mono">{{ address }}</dd></div></dl>
    <ol class="m-0 list-decimal space-y-2 pl-5 text-xs leading-relaxed">
      <li>记下测试设备原来的网关、DNS、IPv6 和安全 DNS 设置。将网关和 DNS 都填为 <code class="font-mono">{{ address }}</code>，设备 IP 保持在原网段。</li>
      <li>暂时关闭测试设备的 IPv6、浏览器安全 DNS 和系统私有 DNS，避免绕过分流。</li>
      <li>分别访问直连站点和代理域名，在 Mihomo 连接列表确认代理访问。</li>
    </ol>
    <details class="text-xs text-muted-foreground"><summary class="min-h-11 cursor-pointer content-center font-medium">测试通过后接入更多设备</summary><p class="mt-1 mb-0 leading-relaxed">支持 DHCP 分发网关与 DNS 的主路由，可将两项都设为 {{ address }}，客户端续租或重新联网后生效。请先确认一台设备测试通过。</p></details>
  </div>
</template>
