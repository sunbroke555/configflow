<script setup lang="ts">
import type { SetupGoal } from '@/api/setup'
import GatewayInstallWizard from '@/components/agents/GatewayInstallWizard.vue'
defineProps<{ profileId: string; goal: SetupGoal }>()
const emit = defineEmits<{ completed: []; busy: [value: boolean] }>()
</script>

<template>
  <GatewayInstallWizard open embedded :profile-id="profileId" profile-name="" :goal="goal" @completed="emit('completed')" @busy="emit('busy', $event)" />
</template>
