<script setup lang="ts">
import type { RuleContentCheck } from '@/lib/ruleset-format'
defineProps<{ check: RuleContentCheck | null }>()
</script>

<template>
  <div v-if="check?.errors.length || check?.warnings?.length" class="space-y-1 text-xs leading-relaxed" data-rule-content-issues>
    <ul v-if="check?.errors.length" role="alert" class="m-0 max-h-40 list-none space-y-1 overflow-y-auto p-0 text-destructive-accent">
      <li v-for="(issue, index) in check.errors" :key="index" class="break-words">{{ issue.line ? `第 ${issue.line} 行：` : '' }}{{ issue.message }}</li>
    </ul>
    <ul v-if="check?.warnings?.length" role="status" class="m-0 max-h-40 list-none space-y-1 overflow-y-auto p-0 text-warning-accent">
      <li v-for="(issue, index) in check.warnings" :key="index" class="break-words">{{ issue.line ? `第 ${issue.line} 行：` : '' }}{{ issue.message }}</li>
    </ul>
  </div>
</template>
