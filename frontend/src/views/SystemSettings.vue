<template>
  <div class="min-w-0 [overflow-wrap:anywhere]">
    <ScopeBanner scope="system" />
    <PageHeader
      title="系统设置"
      description="设置服务地址和访问令牌，备份或恢复全部数据。这里的修改会影响所有配置空间。"
    />
    <!-- ===== 服务配置 / 配置管理 ===== -->
    <div class="grid grid-cols-2 gap-3 max-[1100px]:grid-cols-1">
      <SectionCard title="服务配置" :icon="Settings" class="min-w-0">
        <div class="flex flex-col gap-4">
          <FormField
            label="服务域名"
            html-for="server-domain"
            hint="填写可访问本服务的地址，用于生成规则链接、MosDNS 规则转换地址、Agent 安装脚本和配置订阅链接。"
          >
            <div class="flex items-center gap-1.5">
              <Input
                id="server-domain"
                v-model="serverDomain"
                class="min-w-0 bg-background/50 font-mono"
                placeholder="http://example.com:5001"
                @blur="onServerDomainBlur"
              />
              <Button
                variant="outline"
                size="icon"
                class="shrink-0 border-border/60 bg-background/40"
                title="恢复默认"
                aria-label="恢复默认服务域名"
                @click="resetServerDomain"
              >
                <RefreshCw class="size-4" />
              </Button>
            </div>
          </FormField>

          <FormField
            label="Sub-Store"
            html-for="sub-store-url"
            hint="填写 Sub-Store 的 API 地址，用于读取订阅和转换节点格式。Docker 部署默认 http://sub-store:3001，留空使用环境变量或默认值。"
          >
            <Input
              id="sub-store-url"
              v-model="subStoreUrl"
              class="bg-background/50 font-mono"
              placeholder="http://127.0.0.1:3001"
              @blur="onSubStoreUrlBlur"
            />
          </FormField>

          <FormField hint="开启后将在资源分组下显示「订阅聚合」，可组合订阅和节点。">
            <div class="flex items-center gap-2.5">
              <Switch
                id="agg-enabled"
                v-model="subscriptionAggregationEnabled"
                @update:model-value="value => onSubscriptionAggregationChange(Boolean(value))"
              />
              <Label for="agg-enabled" class="text-[13px] text-muted-foreground">订阅聚合</Label>
            </div>
          </FormField>

          <FormField
            label="访问令牌"
            html-for="config-token"
            hint="所有配置共用此令牌，订阅链接需携带 ?token=xxx。修改后请更新客户端中的链接；清除令牌会关闭令牌保护。"
          >
            <div class="flex items-center gap-1.5">
              <Input
                id="config-token"
                v-model="configToken"
                class="min-w-0 bg-background/50 font-mono"
                placeholder="可手动输入或点击生成"
                @blur="onTokenBlur"
              />
              <Button
                variant="outline"
                size="icon"
                class="shrink-0 border-border/60 bg-background/40"
                title="生成随机令牌"
                aria-label="生成随机令牌"
                @click="generateToken"
              >
                <RefreshCw class="size-4" />
              </Button>
              <Button
                variant="outline"
                size="icon"
                class="shrink-0 border-border/60 bg-background/40"
                title="清除令牌"
                aria-label="清除令牌"
                @click="onClearToken"
              >
                <Trash2 class="size-4" />
              </Button>
            </div>
          </FormField>

          <FormField
            label="MCP"
            html-for="mcp-url"
            hint="在 MCP 客户端中填写此地址，使用 Authorization: Bearer 加上访问令牌认证。此令牌同时授予 MCP 管理权限，请勿公开分享令牌或含令牌的订阅链接。"
          >
            <div class="flex items-center gap-1.5">
              <Input id="mcp-url" :model-value="mcpUrl" readonly class="min-w-0 bg-background/50 font-mono" />
              <Button variant="outline" size="icon" class="shrink-0 border-border/60 bg-background/40" title="复制 MCP 地址" aria-label="复制 MCP 地址" @click="copyMcpUrl">
                <Copy class="size-4" />
              </Button>
            </div>
          </FormField>
        </div>
      </SectionCard>

      <SectionCard title="备份与恢复" :icon="Archive" class="min-w-0">
        <div class="flex flex-col gap-4">
          <div class="grid grid-cols-2 gap-2 max-[480px]:grid-cols-1 [&_button]:h-auto [&_button]:min-h-9 [&_button]:min-w-0 [&_button]:whitespace-normal">
            <Button variant="outline" class="border-border/60 bg-background/40" @click="exportConfig">
              <Upload class="size-4" />
              导出全部数据
            </Button>
            <Button
              variant="outline"
              class="border-border/60 bg-background/40"
              @click="exportConfigDesensitized"
            >
              <ShieldCheck class="size-4" />
              脱敏导出
            </Button>
            <Button variant="outline" class="border-border/60 bg-background/40" @click="pickImportFile">
              <Download class="size-4" />
              导入全部数据
            </Button>
            <Button variant="outline" class="border-border/60 bg-background/40" @click="handleBackup">
              <CloudUpload class="size-4" />
              WebDAV 备份
            </Button>
            <input
              ref="importInput"
              type="file"
              accept=".json"
              hidden
              @change="onImportFileChange"
            />
          </div>

          <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">
            导出和 WebDAV 备份包含共享资源、所有配置、系统设置及 Agent。脱敏导出隐藏敏感信息，仅用于分享。导入会覆盖上述全部数据，不仅影响当前配置，且无法撤销，请先备份。完整备份含敏感信息，请妥善保管。
          </p>

          <!-- 重置会清空全部数据，与常规操作分区并降低视觉权重，避免误触 -->
          <div
            class="mt-auto flex flex-wrap items-center gap-3 rounded-lg border border-destructive-accent/25 bg-destructive-soft/30 p-3"
          >
            <div class="min-w-0 flex-[1_1_12rem]">
              <p class="m-0 text-[13px] font-semibold text-destructive-accent">重置全部数据</p>
              <p class="mt-0.5 mb-0 text-[12px] text-muted-foreground">
                清空共享资源、所有配置及 Agent，恢复默认系统设置，不可撤销。
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              class="shrink-0 border-destructive-accent/40 bg-transparent text-destructive-accent"
              @click="resetConfig"
            >
              <RotateCcw class="size-3.5" />
              重置
            </Button>
          </div>
        </div>
      </SectionCard>
    </div>

    <!-- ===== 配置备份 ===== -->
    <Dialog v-model:open="backupDialogVisible">
      <DialogContent class="max-w-[620px] [overflow-wrap:anywhere]">
        <DialogHeader>
          <DialogTitle>备份全部数据</DialogTitle>
          <DialogDescription>
            通过 WebDAV 备份共享资源、所有配置、系统设置及 Agent。支持坚果云、Nextcloud 等服务。
          </DialogDescription>
        </DialogHeader>

        <div class="flex max-h-[56dvh] flex-col gap-4 overflow-y-auto pr-1">
          <FormField
            label="WebDAV 地址"
            html-for="webdav-url"
            hint="例如坚果云：https://dav.jianguoyun.com/dav/"
          >
            <Input
              id="webdav-url"
              v-model="backupForm.webdav_url"
              class="bg-background/50 font-mono"
              placeholder="https://dav.jianguoyun.com/dav/"
            />
          </FormField>

          <FormField label="用户名" html-for="webdav-user">
            <Input
              id="webdav-user"
              v-model="backupForm.webdav_username"
              autocomplete="username"
              class="bg-background/50"
              placeholder="WebDAV 用户名 / 邮箱"
            />
          </FormField>

          <FormField label="密码" html-for="webdav-pass" hint="坚果云需要使用应用密码，不是登录密码。">
            <Input
              id="webdav-pass"
              v-model="backupForm.webdav_password"
              type="password"
              autocomplete="current-password"
              class="bg-background/50"
              placeholder="WebDAV 密码 / 应用密码"
            />
          </FormField>

          <FormField label="备份路径" html-for="webdav-path" hint="备份在 WebDAV 中的保存位置，默认为 /config-flow-backup/">
            <Input
              id="webdav-path"
              v-model="backupForm.webdav_path"
              class="bg-background/50 font-mono"
              placeholder="/config-flow-backup/"
            />
          </FormField>

          <FormField hint="开启后，每次修改配置都会自动备份全部数据。">
            <div class="flex items-center gap-2.5">
              <Switch id="auto-backup" v-model="backupForm.auto_backup" />
              <Label for="auto-backup" class="text-[13px] text-muted-foreground">自动备份</Label>
            </div>
          </FormField>
        </div>

        <DialogFooter class="gap-2 sm:flex-wrap sm:justify-between [&_button]:h-auto [&_button]:min-h-9 [&_button]:min-w-0 [&_button]:whitespace-normal">
          <div class="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
            <Button
              variant="outline"
              class="border-border/60 bg-background/40"
              :disabled="testingConnection"
              @click="testWebDAVConnection"
            >
              <Loader2 v-if="testingConnection" class="size-4 animate-spin" />
              测试连接
            </Button>
            <Button variant="outline" class="border-border/60 bg-background/40" :disabled="backingUp" @click="backupNow">
              <Loader2 v-if="backingUp" class="size-4 animate-spin" />
              立即备份
            </Button>
          </div>
          <div class="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
            <Button variant="outline" @click="backupDialogVisible = false">取消</Button>
            <Button :disabled="savingBackup" @click="saveBackupConfig">
              <Loader2 v-if="savingBackup" class="size-4 animate-spin" />
              保存配置
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { isAxiosError } from 'axios'
import { Archive, CloudUpload, Copy, Download, Loader2, RefreshCw, RotateCcw, Settings, ShieldCheck, Trash2, Upload } from '@lucide/vue'
import PageHeader from '@/components/common/PageHeader.vue'
import ScopeBanner from '@/components/shell/ScopeBanner.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import FormField from '@/components/common/FormField.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import api, { configApi, serverDomainApi, configTokenApi, subStoreUrlApi } from '@/api'

// 备份配置
const backupDialogVisible = ref(false)
const backupForm = ref({
  webdav_url: '',
  webdav_username: '',
  webdav_password: '',
  webdav_path: '/config-flow-backup/',
  auto_backup: false
})
const testingConnection = ref(false)
const backingUp = ref(false)
const savingBackup = ref(false)

// Sub-Store URL
const subStoreUrl = ref('')

// 订阅聚合开关
const subscriptionAggregationEnabled = ref(false)

const serverDomain = ref(window.location.origin)
const configToken = ref('')
const mcpUrl = computed(() => `${serverDomain.value.replace(/\/+$/, '')}/mcp`)

const copyMcpUrl = async () => {
  try {
    await navigator.clipboard.writeText(mcpUrl.value)
    notify.success('MCP 地址已复制')
  } catch {
    notify.error('复制失败，请手动复制')
  }
}

const loadConfigToken = async () => {
  try {
    const response = await configTokenApi.get()
    configToken.value = response.data.config_token || ''
  } catch {
    notify.error('加载访问令牌失败')
  }
}

const handleBackup = () => {
  showBackupDialog()
}

// 重置服务域名为当前浏览器地址
const resetServerDomain = async () => {
  const ok = await confirm('是否将服务域名重置为当前浏览器地址？', { title: '重置服务域名' })
  if (!ok) return

  try {
    const newDomain = window.location.origin

    await serverDomainApi.update({
      new_domain: newDomain
    })

    serverDomain.value = newDomain
    localStorage.setItem('serverDomain', newDomain)

    notify.success('服务域名已重置为当前地址')
  } catch (error) {
    console.error('重置服务域名失败:', error)
    notify.error('重置服务域名失败')
  }
}

// 输入框失去焦点时保存
const onServerDomainBlur = async () => {
  if (!serverDomain.value) {
    return
  }

  try {
    await serverDomainApi.update({
      new_domain: serverDomain.value
    })
    localStorage.setItem('serverDomain', serverDomain.value)

    notify.success(`服务域名已更新为：${serverDomain.value}`)
  } catch (error) {
    console.error('更新服务域名失败:', error)
    notify.error('更新服务域名失败')
  }
}

// 订阅聚合开关变化处理
const onSubscriptionAggregationChange = async (value: boolean) => {
  try {
    // 保存到后端
    await api.post('/settings/subscription-aggregation', {
      enabled: value
    })
    localStorage.setItem('subscriptionAggregationEnabled', value.toString())

    notify.success(value ? '订阅聚合已开启' : '订阅聚合已关闭')

    // 触发自定义事件，通知其他组件更新
    window.dispatchEvent(new CustomEvent('subscription-aggregation-changed', {
      detail: { enabled: value }
    }))
  } catch (error) {
    console.error('更新订阅聚合开关失败:', error)
    notify.error('更新订阅聚合设置失败')
    // 失败时恢复原值
    subscriptionAggregationEnabled.value = !value
  }
}

const generateToken = async () => {
  try {
    const response = await configTokenApi.update({ generate: true })
    configToken.value = response.data.config_token
    notify.success('令牌已生成并保存')
  } catch (error) {
    console.error('生成令牌失败:', error)
    notify.error('生成令牌失败')
  }
}

const onTokenBlur = async () => {
  // 如果输入框为空，不保存
  if (!configToken.value || configToken.value.trim() === '') {
    return
  }

  try {
    await configTokenApi.update({ token: configToken.value })
    notify.success('令牌已保存')
  } catch (error) {
    console.error('保存令牌失败:', error)
    notify.error('保存令牌失败')
  }
}

const onClearToken = async () => {
  const ok = await confirm('确定清除访问令牌吗？清除后，所有配置的订阅链接都不再需要令牌验证，MCP 也不再受此令牌保护。', {
    title: '清除令牌',
    confirmText: '清除'
  })
  if (!ok) return

  try {
    await configTokenApi.delete()
    configToken.value = ''
    notify.success('令牌已清除')
  } catch (error) {
    console.error('清除令牌失败:', error)
    notify.error('清除令牌失败')
  }
}

// Sub-Store URL 相关函数
const onSubStoreUrlBlur = async () => {
  try {
    await subStoreUrlApi.update({
      sub_store_url: subStoreUrl.value
    })

    notify.success('Sub-Store URL 已保存')
  } catch (error) {
    console.error('保存 Sub-Store URL 失败:', error)
    notify.error('保存 Sub-Store URL 失败')
  }
}

const loadSubStoreUrl = async () => {
  try {
    const response = await subStoreUrlApi.get()
    subStoreUrl.value = response.data.sub_store_url || ''
  } catch (error) {
    console.error('加载 Sub-Store URL 失败:', error)
    notify.error('加载 Sub-Store URL 失败')
  }
}

const exportConfig = async () => {
  try {
    const response = await configApi.export()

    const blob = new Blob([response.data], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'config.json'
    link.click()
    window.URL.revokeObjectURL(url)

    notify.success('全部数据已导出，请妥善保管备份文件')
  } catch (error) {
    notify.error('导出失败')
  }
}

const exportConfigDesensitized = async () => {
  try {
    const response = await configApi.exportDesensitized()

    const blob = new Blob([response.data], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'config_desensitized.json'
    link.click()
    window.URL.revokeObjectURL(url)

    notify.success('脱敏数据已导出，仅用于分享')
  } catch (error) {
    notify.error('导出失败')
  }
}

const importInput = ref<HTMLInputElement | null>(null)

const pickImportFile = () => importInput.value?.click()

const onImportFileChange = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  try {
    const config = JSON.parse(await file.text())
    const ok = await confirmDanger(
      '导入将覆盖共享资源、所有配置、系统设置及 Agent，不仅影响当前配置，且无法撤销。请先导出备份。确定继续吗？',
      { title: '导入全部数据', confirmText: '确认覆盖并导入' }
    )
    if (!ok) return
    await configApi.import(config)
    notify.success('全部数据已导入，正在刷新页面')
    localStorage.removeItem('serverDomain')
    localStorage.removeItem('subscriptionAggregationEnabled')
    window.location.reload()
  } catch (error) {
    console.error('导入配置失败:', error)
    notify.error('导入失败，请检查文件格式')
  } finally {
    input.value = ''
  }
}

const resetConfig = async () => {
  const ok = await confirmDanger(
    '重置将清空共享资源、所有配置及 Agent，并恢复默认系统设置。不仅影响当前配置，且无法撤销。请先导出备份。',
    { title: '重置全部数据', confirmText: '确认重置' }
  )
  if (!ok) return

  try {
    await api.post('/config/reset')
    notify.success('全部数据已重置，正在刷新页面')
    localStorage.removeItem('serverDomain')
    localStorage.removeItem('subscriptionAggregationEnabled')

    // 刷新统计
    setTimeout(() => {
      // 刷新页面以加载新配置
      window.location.reload()
    }, 500)
  } catch (error) {
    if (error !== 'cancel') {
      notify.error('重置失败')
    }
  }
}

// 备份相关方法
const showBackupDialog = async () => {
  try {
    // 加载备份配置
    const response = await api.get('/backup/config')
    if (response.data) {
      backupForm.value = {
        webdav_url: response.data.webdav_url || '',
        webdav_username: response.data.webdav_username || '',
        webdav_password: response.data.webdav_password || '',
        webdav_path: response.data.webdav_path || '/config-flow-backup/',
        auto_backup: response.data.auto_backup || false
      }
    }
    backupDialogVisible.value = true
  } catch (error) {
    console.error('加载备份配置失败', error)
    notify.error('加载备份配置失败')
  }
}

const testWebDAVConnection = async () => {
  if (!backupForm.value.webdav_url) {
    notify.warning('请输入 WebDAV 地址')
    return
  }
  if (!backupForm.value.webdav_username) {
    notify.warning('请输入用户名')
    return
  }
  if (!backupForm.value.webdav_password) {
    notify.warning('请输入密码')
    return
  }

  try {
    testingConnection.value = true
    await api.post('/backup/test', {
      webdav_url: backupForm.value.webdav_url,
      webdav_username: backupForm.value.webdav_username,
      webdav_password: backupForm.value.webdav_password,
      webdav_path: backupForm.value.webdav_path
    })
    notify.success('连接测试成功')
  } catch (error) {
    console.error('测试连接失败', error)
    const errorMsg = isAxiosError<{ message?: string }>(error) ? error.response?.data?.message : undefined
    notify.error(errorMsg || '连接失败，请检查 WebDAV 地址、用户名和密码')
  } finally {
    testingConnection.value = false
  }
}

const backupNow = async () => {
  if (!backupForm.value.webdav_url) {
    notify.warning('请输入 WebDAV 地址')
    return
  }
  if (!backupForm.value.webdav_username) {
    notify.warning('请输入用户名')
    return
  }
  if (!backupForm.value.webdav_password) {
    notify.warning('请输入密码')
    return
  }

  try {
    backingUp.value = true
    await api.post('/backup/now', {
      webdav_url: backupForm.value.webdav_url,
      webdav_username: backupForm.value.webdav_username,
      webdav_password: backupForm.value.webdav_password,
      webdav_path: backupForm.value.webdav_path
    })
    notify.success('全部数据已备份到 WebDAV')
  } catch (error) {
    console.error('备份失败', error)
    const errorMsg = isAxiosError<{ message?: string }>(error) ? error.response?.data?.message : undefined
    notify.error(errorMsg || '备份失败，请检查 WebDAV 连接和备份路径')
  } finally {
    backingUp.value = false
  }
}

const saveBackupConfig = async () => {
  try {
    savingBackup.value = true
    await api.post('/backup/config', backupForm.value)
    notify.success('备份配置已保存')
    backupDialogVisible.value = false
  } catch (error) {
    console.error('保存备份配置失败', error)
    notify.error('保存备份设置失败')
  } finally {
    savingBackup.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    loadConfigToken(),
    loadSubStoreUrl(),
    (async () => {
      try {
        const response = await serverDomainApi.get()
        serverDomain.value = response.data.server_domain?.trim() || window.location.origin
        localStorage.setItem('serverDomain', serverDomain.value)
      } catch {
        notify.error('加载服务域名失败')
      }
    })(),
    (async () => {
      try {
        const response = await api.get('/settings/subscription-aggregation')
        subscriptionAggregationEnabled.value = Boolean(response.data.enabled)
        localStorage.setItem('subscriptionAggregationEnabled', String(subscriptionAggregationEnabled.value))
      } catch {
        notify.error('加载订阅聚合开关失败')
      }
    })()
  ])
})
</script>
