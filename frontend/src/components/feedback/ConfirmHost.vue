<template>
  <AlertDialog :open="confirmState.open" @update:open="value => !value && settleConfirm('cancel')">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>{{ confirmState.title }}</AlertDialogTitle>
        <AlertDialogDescription class="whitespace-pre-line">
          {{ confirmState.description }}
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel @click="settleConfirm('cancel')">
          {{ confirmState.cancelText }}
        </AlertDialogCancel>
        <!-- 先在捕获阶段结算选择，避免 Reka 的自动关闭先将 Promise 结算为取消。
             第三个按钮只在调用方给了 altText 时出现。 -->
        <AlertDialogAction
          v-if="confirmState.altText"
          class="bg-secondary text-secondary-foreground hover:bg-secondary/80"
          @click.capture="settleConfirm('alt')"
        >
          {{ confirmState.altText }}
        </AlertDialogAction>
        <AlertDialogAction
          :class="confirmState.danger ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : ''"
          @click.capture="settleConfirm('confirm')"
        >
          {{ confirmState.confirmText }}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
</template>

<script setup lang="ts">
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle
} from '@/components/ui/alert-dialog'
import { confirmState, settleConfirm } from '@/lib/feedback'
</script>
