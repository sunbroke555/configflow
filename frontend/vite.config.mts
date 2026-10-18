import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'
import { resolve } from 'path'

export function frontendVersionPlugin(): Plugin {
  return {
    name: 'configflow-frontend-version',
    apply: 'build',
    generateBundle(_options, bundle) {
      const entries = Object.values(bundle).filter(chunk => chunk.type === 'chunk' && chunk.isEntry)
      if (entries.length !== 1) {
        this.error('ConfigFlow frontend version requires exactly one entry chunk')
      }
      this.emitFile({
        type: 'asset',
        fileName: 'frontend-version.json',
        source: JSON.stringify({ entry: entries[0].fileName }) + '\n'
      })
    }
  }
}

export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    frontendVersionPlugin(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon.png'],
      manifest: {
        name: 'ConfigFlow',
        short_name: 'ConfigFlow',
        description: '代理配置管理：订阅、节点、策略与配置生成',
        lang: 'zh-CN',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        orientation: 'portrait',
        background_color: '#191817',
        theme_color: '#191817',
        icons: [
          { src: 'icon.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' }
        ]
      },
      workbox: {
        // 应用壳与静态资源预缓存；接口数据始终走网络，避免展示过期配置
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        globIgnores: ['**/frontend-version.json'],
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        navigateFallbackDenylist: [
          /^\/api(?:\/|$)/,
          /^\/assets(?:\/|$)/,
          /^\/frontend-version\.json(?:\?|$)/,
          /[?&]__cf_reload=/
        ],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname === '/frontend-version.json',
            handler: 'NetworkOnly'
          },
          {
            urlPattern: ({ url }) => url.pathname.startsWith('/api/'),
            handler: 'NetworkOnly'
          }
        ]
      },
      devOptions: { enabled: false }
    })
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    port: 3001,
    proxy: {
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true
      }
    }
  }
})
