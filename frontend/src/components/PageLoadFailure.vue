<script setup lang="ts">
import { Button } from '@/components/ui/button'
const retry = () => window.location.reload()
</script>

<template>
  <main class="min-h-screen flex items-center justify-center bg-background p-6">
    <section class="w-full max-w-md rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
      <h1 class="text-xl font-semibold text-foreground">页面暂时无法打开</h1>
      <p class="mt-3 text-sm leading-6 text-muted-foreground">可能正在更新或网络暂时不稳定，请稍后重试。</p>
      <Button class="mt-6" @click="retry">重新打开</Button>
    </section>
  </main>
</template>
