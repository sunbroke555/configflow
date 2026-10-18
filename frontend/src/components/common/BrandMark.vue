<template>
  <!-- 品牌标记：放射状光芒，busy 时持续旋转表示正在处理 -->
  <svg
    viewBox="0 0 32 32"
    class="brand-mark overflow-visible"
    :class="busy && 'is-busy'"
    aria-hidden="true"
  >
    <g class="brand-mark__rays" :class="busy && 'cf-loading-spin'" stroke="currentColor" stroke-linecap="round" fill="none">
      <path d="M16 16 L16 2.5" stroke-width="3.2" />
      <path d="M16 16 L25.5 6.5" stroke-width="2.4" />
      <path d="M16 16 L29.5 16" stroke-width="3.2" />
      <path d="M16 16 L26.6 24.2" stroke-width="2" />
      <path d="M16 16 L16 29.5" stroke-width="3.2" />
      <path d="M16 16 L6.5 25.5" stroke-width="2.4" />
      <path d="M16 16 L2.5 16" stroke-width="3.2" />
      <path d="M16 16 L5.4 7.8" stroke-width="2" />
      <path d="M16 16 L20.6 3.6" stroke-width="1.4" />
      <path d="M16 16 L28.4 20.6" stroke-width="1.4" />
      <path d="M16 16 L11.4 28.4" stroke-width="1.4" />
      <path d="M16 16 L3.6 11.4" stroke-width="1.4" />
    </g>
  </svg>
</template>

<script setup lang="ts">
defineProps<{ busy?: boolean }>()
</script>

<style scoped>
.brand-mark__rays {
  transform-origin: 16px 16px;
  transition: transform 1.2s var(--ease-flow);
}

/* 不能写 :global(.group:hover) .x —— Vue 会把整条选择器编译成 .group:hover，让所有 group 元素旋转 */
.group:hover .brand-mark__rays {
  transform: rotate(90deg);
}

.is-busy .brand-mark__rays {
  --cf-loading-spin-duration: 1.6s;
  animation: brand-spin var(--cf-loading-spin-duration) linear infinite;
}

@keyframes brand-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .brand-mark__rays {
    transition: none;
  }

  .is-busy .brand-mark__rays {
    animation: none;
  }
}
</style>
