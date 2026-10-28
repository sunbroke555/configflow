import DefaultTheme from 'vitepress/theme'
import type { Theme } from 'vitepress'
import DocsTabs from './DocsTabs.vue'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('DocsTabs', DocsTabs)
  },
} satisfies Theme
