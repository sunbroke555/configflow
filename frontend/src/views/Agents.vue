<template>
  <div>

    <PageHeader
      title="Agent"
      class="agents-page-header"
    >
      <template #actions>
        <Button variant="outline" class="border-border/60 bg-background/40 max-sm:size-11 max-sm:shrink-0 max-sm:gap-0" aria-label="刷新 Agent" title="刷新 Agent" @click="loadAgents">
          <RefreshCw class="size-4" />
          <span class="max-sm:sr-only">刷新</span>
        </Button>
        <Button variant="outline" class="border-border/60 bg-background/40" @click="handleGenerateScript">
          <FileText class="size-4" />
          <span><span class="max-sm:sr-only">生成</span>安装脚本</span>
        </Button>
        <Button class="shadow-glow" :disabled="pushingAll || !onlineCount" @click="pushAll">
          <Loader2 v-if="pushingAll" class="size-4 animate-spin" />
          <Send v-else class="size-4" />
          推送到全部
        </Button>
      </template>
    </PageHeader>

    <SectionCard v-if="agents.length === 0" :padded="false">
      <EmptyState
        :icon="Server"
        title="暂无 Agent"
        description="生成安装脚本并在目标机器上执行，Agent 注册后会出现在这里。"
      >
        <Button @click="handleGenerateScript">
          <FileText class="size-4" />
          生成安装脚本
        </Button>
      </EmptyState>
    </SectionCard>

    <template v-else>
      <!-- 拓扑：ConfigFlow → 各 Agent，每台设备一条下发通道 -->
      <details :open="!isNarrow" class="group/topology mb-3.5" data-agent-topology>
        <summary class="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl border border-border bg-card px-3 text-[13px] font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:hidden [&::-webkit-details-marker]:hidden">
          <span>下发拓扑</span>
          <span class="ml-auto text-xs font-normal text-muted-foreground">{{ agents.length }} 台 · {{ onlineCount }} 在线</span>
          <ChevronDown class="size-4 shrink-0 text-muted-foreground transition-transform group-open/topology:rotate-180" aria-hidden="true" />
        </summary>
        <SectionCard :padded="false" class="max-sm:mt-2" role="region" aria-label="下发拓扑">
          <FlowMap
            ref="topology"
            label="下发拓扑"
            :columns="topoColumns"
            :edges="topoEdges"
            :height="Math.max(260, agents.length * 72)"
            :min-width="640"
            columns-template="minmax(0,1fr) minmax(0,1.2fr)"
            :gap-x="200"
          >
            <template #node="{ node }">
              <span v-if="node.kind === 'hub'" class="group flex flex-col items-start p-[18px]">
                <BrandMark class="size-10 text-primary" :busy="pushingAll" />
                <span class="font-display mt-2.5 text-[22px] font-medium">ConfigFlow</span>
                <span class="mt-1 max-w-full text-[10.5px] text-muted-foreground [overflow-wrap:anywhere]" :title="`当前配置修订号：${activeRevision === undefined ? '暂未获取' : `v${activeRevision}`}`">
                  配置空间 · {{ activeProfileName }}
                </span>
              </span>
            </template>
          </FlowMap>
        </SectionCard>
      </details>

      <div class="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3.5 max-md:grid-cols-1">
        <Motion
          v-for="(agent, index) in agents"
          :key="agent.id"
          v-bind="listItem(index)"
          :class="cn(
            'relative flex flex-col overflow-hidden rounded-[18px] border border-border bg-card/90 p-[18px] shadow-surface max-sm:p-3',
            agent.status !== 'online' && !deployments[agent.id] && !upgrades[agent.id] && !agent.dns_follow && 'opacity-80'
          )"
        >
          <header class="flex items-center gap-2.5">
            <span
              :class="cn('size-[9px] shrink-0 rounded-full', agent.status === 'online' ? 'agent-pulse bg-success-accent' : 'bg-muted-foreground/60')"
              role="img"
              :aria-label="agent.status === 'online' ? '在线' : '离线'"
              :title="agent.status === 'online' ? '在线' : '离线'"
            />
            <p class="font-display m-0 min-w-0 flex-1 truncate text-[19px] font-medium" :title="agent.name">{{ agent.name }}</p>
            <Badge variant="outline" :class="cn('chip font-mono', ['mosdns', 'oxidns'].includes(agent.service_type) && 'chip-sky')">{{ serviceTypeLabels[agent.service_type] || agent.service_type }}</Badge>
            <span class="font-mono text-[11px] text-muted-foreground">{{ agent.version || '' }}</span>
          </header>
          <p class="mt-1 mb-0 truncate font-mono text-[11.5px] text-muted-foreground">
            {{ agent.host }}:{{ agent.port }}
            · {{ agent.deployment_method === 'shell' ? 'shell' : agent.deployment_method === 'docker' ? 'docker' : agent.deployment_method || '—' }}
            · 心跳 {{ formatTime(agent.last_heartbeat) }}
          </p>

          <div class="mt-3 flex items-center gap-2" data-agent-monitor-actions>
            <Button
              variant="outline"
              size="sm"
              class="min-w-0 flex-1 border-border-strong bg-secondary/60 text-xs text-foreground only:flex-none"
              :aria-label="`查看 ${agent.name} 的系统监控`"
              @click="showMetricsDetail(agent)"
            >
              <ChartLine class="size-3.5" />
              系统监控
            </Button>
            <Button
              v-if="agent.service_type === 'oxidns'"
              variant="outline"
              size="sm"
              class="min-w-0 flex-1 border-border-strong bg-secondary/60 text-xs text-foreground only:flex-none"
              :aria-label="`查看 ${agent.name} 的 OxiDNS 面板`"
              @click="router.push('/agents/' + encodeURIComponent(agent.id) + '/oxidns')"
            >
              <ChartLine class="size-3.5" />
              OxiDNS 面板
            </Button>
            <Button
              v-if="agent.service_type === 'mihomo'"
              variant="outline"
              size="sm"
              class="min-w-0 flex-1 border-border-strong bg-secondary/60 text-xs text-foreground only:flex-none"
              :aria-label="`查看 ${agent.name} 的 Mihomo 面板`"
              @click="router.push({ name: 'MihomoPanel', query: { agent: agent.id } })"
            >
              <ChartLine class="size-3.5" />
              Mihomo 面板
            </Button>
          </div>

          <!-- 资源占用使用百分比；网络速率单独展示趋势。 -->
          <div class="my-3 grid grid-cols-[44px_minmax(0,1fr)_max-content] gap-2.5 max-sm:gap-2">
            <div
              v-for="metric in metricsOf(agent)"
              :key="metric.label"
              class="col-span-3 grid grid-cols-subgrid items-center gap-2.5 text-[11.5px] text-muted-foreground"
              :title="metric.detail || undefined"
            >
              {{ metric.label }}
              <i class="block h-1.5 overflow-hidden rounded-full bg-accent" aria-hidden="true">
                <span
                  class="block h-full rounded-full transition-[width] duration-1000 ease-(--ease-flow)"
                  :style="{ width: `${agent.system_metrics ? Math.min(100, metric.percent) : 0}%`, minWidth: metric.percent > 0 ? '2px' : undefined, background: metric.color }"
                />
              </i>
              <b class="whitespace-nowrap text-right font-mono font-medium text-foreground/80">{{ agent.system_metrics ? metric.text : '—' }}</b>
            </div>
          </div>
          <AgentTraffic :agent="agent" :now="trafficNow" />

          <div class="mb-3.5 flex items-center gap-2 text-[11.5px] text-muted-foreground">
            配置空间
            <Select
              :model-value="agent.profile_id || defaultProfileId"
              :disabled="bindingAgentId === agent.id || settingsLoadingAgentId === agent.id || profileBindingBlocked(agent.id)"
              @update:model-value="value => handleAgentProfileChange(agent, String(value))"
            >
              <SelectTrigger class="h-7 min-w-0 flex-1 bg-background/50 text-[12px]" :aria-label="`${agent.name} 绑定的配置空间`">
                <SelectValue />
              </SelectTrigger>
              <SelectContent class="glass-strong">
                <SelectItem v-for="profile in profiles" :key="profile.id" :value="profile.id">
                  {{ profile.name }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div class="mb-3 min-w-0 divide-y divide-border/70 border-t border-border/70">
            <AgentStatusRow v-if="submissionFailures[agent.id]" label="推送请求" status="失败" tone="danger">
              <template #notice>
                <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="submissionFailures[agent.id]" />
                <AgentDNSChecks :checks="submissionFailures[agent.id].dns_checks" :checked-at="submissionFailures[agent.id].dns_checked_at" compact />
              </template>
            </AgentStatusRow>
            <!-- 成功记录收起；正在执行的步骤、失败原因与恢复操作始终可见。 -->
            <AgentDeploymentStatus v-if="deployments[agent.id]" :task="deployments[agent.id]" compact>
              <template v-if="deploymentNeedsNotice(deployments[agent.id])" #notice>
                <AgentOperationSteps :task="deployments[agent.id]" :busy="deploymentBusy(agent.id)" :show-current="false" :show-steps="false" />
                <p v-if="deployments[agent.id].not_recorded && !deployments[agent.id].manual_rollback" class="m-0 text-warning-accent">后端暂未记录此发布，正在继续查询。可使用原发布编号重新提交。</p>
                <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="deployments[agent.id]" />
                <Button v-if="deployments[agent.id].status === 'ready'" variant="outline" size="sm" class="min-h-11" @click="activateDeployment(agent.id)">应用并启动</Button>
                <Button v-if="deployments[agent.id].status === 'rollback_failed'" variant="outline" size="sm" class="min-h-11" @click="refreshDeployment(agent.id)">查询恢复状态</Button>
                <Button v-if="!deployments[agent.id].manual_rollback && (deployments[agent.id].not_recorded || deployments[agent.id].retrying)" variant="outline" size="sm" class="min-h-11" :disabled="deployments[agent.id].retrying" @click="retryDeployment(agent.id)">{{ deployments[agent.id].retrying ? '正在重新提交…' : '重试同一次发布' }}</Button>
              </template>
              <template #details>
                <AgentOperationSteps :task="deployments[agent.id]" :busy="deploymentBusy(agent.id)" :show-current="false" :show-message="!deploymentNeedsNotice(deployments[agent.id])" embedded />
              </template>
            </AgentDeploymentStatus>
            <AgentDNSChecks v-if="deployments[agent.id]" :key="deployments[agent.id].deployment_id" :checks="deployments[agent.id].dns_checks" :checked-at="deployments[agent.id].dns_checked_at" compact />

            <AgentDNSFollow
              v-if="agent.service_type === 'oxidns'"
              :agent="agent"
              :source-name="agents.find(item => item.id === agent.dns_follow?.source_agent_id)?.name"
              :blocked="deploymentBusy(agent.id) || upgradeBusy(agent.id) || pushingIds.has(agent.id)"
              @changed="loadAgents"
            />

            <AgentUpgradeStatus v-if="upgrades[agent.id]" :task="upgrades[agent.id]" :busy="upgradeBusy(agent.id)">
              <template #actions>
                <Button v-if="!upgrades[agent.id].update_id && upgrades[agent.id].status === 'unknown'" variant="outline" size="sm" class="min-h-11" :disabled="upgradeQuerying.has(agent.id)" @click="retryUnconfirmedUpgrade(agent.id)">重新确认并提交更新</Button>
                <Button
                  v-if="upgradeBusy(agent.id)"
                  variant="outline"
                  size="sm"
                  class="min-h-11"
                  :disabled="upgradeQuerying.has(agent.id)"
                  @click="queryUpgrade(agent.id)"
                >
                  <Loader2 v-if="upgradeQuerying.has(agent.id)" class="size-3.5 animate-spin" />
                  {{ upgradeQuerying.has(agent.id) ? '正在查询…' : '查询更新结果' }}
                </Button>
              </template>
            </AgentUpgradeStatus>
          </div>

          <div v-if="agent.has_update" class="mb-3 flex items-center gap-2 rounded-xl border border-primary-accent/30 bg-primary-soft/40 px-3 py-2 text-[12px]">
            <span class="min-w-0 flex-1 text-muted-foreground">
              {{ agent.deployment_method === 'docker'
                ? '有新版 Agent，按步骤拉取镜像并重建容器'
                : agent.upgrade_available ? '有新版 Agent 可在线更新' : '更新文件或安装方式尚未就绪，请先确认 ConfigFlow 主服务已更新' }}
            </span>
            <Button
              variant="outline"
              size="sm"
              class="h-7 shrink-0"
              :disabled="agent.deployment_method !== 'docker' && (!agent.upgrade_available || upgradeBusy(agent.id) || deploymentBusy(agent.id))"
              @click="updateAgent(agent)"
            >
              <Download class="size-3.5" />
              更新
            </Button>
          </div>

          <AgentRuleSync v-if="['mihomo', 'oxidns'].includes(agent.service_type)" :agent-id="agent.id" />

          <footer class="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3.5 max-sm:pt-2">
            <span
              :class="cn('mr-auto min-w-0 text-[12px]', syncOf(agent).tone === 'success' ? 'text-success-accent' : syncOf(agent).tone === 'warning' ? 'text-warning-accent' : 'text-muted-foreground')"
              :title="syncOf(agent).title"
            >
              {{ syncOf(agent).label }}
            </span>
            <Button
              v-if="supportsAgentSettings(agent.service_type)"
              variant="outline"
              size="sm"
              :aria-label="`设置 ${agent.name} 的配置`"
              :title="`编辑「${profileStore.profileName(agent.profile_id || defaultProfileId)}」中的 ${serviceTypeLabels[agent.service_type]} 配置`"
              :disabled="Boolean(settingsLoadingAgentId) || bindingAgentId === agent.id"
              @click="openAgentSettings(agent)"
            >
              <Loader2 v-if="settingsLoadingAgentId === agent.id" class="size-3.5 animate-spin" />
              <Settings v-else class="size-3.5" />
              设置
            </Button>
            <Button
              v-if="agent.status === 'online'"
              variant="outline"
              size="sm"
              :disabled="pushingIds.has(agent.id) || upgradeBusy(agent.id) || deploymentBusy(agent.id) || deployments[agent.id]?.status === 'rollback_failed'"
              @click="pushConfig(agent)"
            >
              <Loader2 v-if="pushingIds.has(agent.id) || deploymentBusy(agent.id)" class="size-3.5 animate-spin" />
              <Send v-else class="size-3.5" />
              推送
            </Button>
            <OxiDNSRuleUpdate v-if="agent.service_type === 'oxidns'" :agent-id="agent.id"
              :disabled="agent.status !== 'online' || pushingIds.has(agent.id) || upgradeBusy(agent.id) || deploymentBusy(agent.id) || deployments[agent.id]?.status === 'rollback_failed'"
              @updated="loadAgents" />
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <Button variant="ghost" size="icon-sm" :aria-label="`${agent.name} 的更多操作`">
                  <MoreHorizontal class="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" class="glass-strong min-w-[160px]">
                <DropdownMenuItem v-if="['mihomo', 'mosdns', 'oxidns'].includes(agent.service_type)" @select="deploymentHistoryAgentId = agent.id">
                  <History class="size-4" />
                  发布记录与回滚
                </DropdownMenuItem>
                <DropdownMenuItem @select="viewLogs(agent)">
                  <ScrollText class="size-4" />
                  日志
                </DropdownMenuItem>
                <DropdownMenuItem v-if="agent.system_metrics" @select="showMetricsDetail(agent)">
                  <ChartLine class="size-4" />
                  系统监控
                </DropdownMenuItem>
                <DropdownMenuItem
                  :disabled="upgradeBusy(agent.id) || deploymentBusy(agent.id) || deployments[agent.id]?.status === 'rollback_failed'"
                  @select="restartAgent(agent)"
                >
                  <RotateCw class="size-4" />
                  重启服务
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem variant="destructive" @select="uninstallAgent(agent)">
                  <Trash2 class="size-4" />
                  卸载 Agent
                </DropdownMenuItem>
                <DropdownMenuItem :disabled="!isHeartbeatExpired(agent)" @select="deleteAgent(agent)">
                  <X class="size-4" />
                  删除记录
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </footer>
        </Motion>
      </div>
    </template>

    <component
      :is="agentSettingsEditor"
      v-if="agentSettings && agentSettingsEditor"
      :key="agentSettings.agentId + ':' + agentSettings.profileId"
      :agent-settings="agentSettings"
      @ready="settingsLoadingAgentId = null"
      @close="closeAgentSettings"
    />
    <DockerAgentUpdateDialog v-if="dockerUpdateAgent" :agent="dockerUpdateAgent" @close="dockerUpdateAgent = null" @refresh="loadAgents" />
    <AgentDeploymentHistory v-if="deploymentHistoryAgent" :agent="deploymentHistoryAgent" :progress="deployments[deploymentHistoryAgent.id]" :busy="deploymentBusy(deploymentHistoryAgent.id)" :blocked="upgradeBusy(deploymentHistoryAgent.id) || pushingIds.has(deploymentHistoryAgent.id) || rollbackConfirming" @close="deploymentHistoryAgentId = null" @rollback="requestRollback(deploymentHistoryAgent, $event)" />

    <GatewayInstallWizard
      v-if="gatewayInstallContext"
      :open="true"
      :profile-id="gatewayInstallContext.profileId"
      :profile-name="gatewayInstallContext.profileName"
      @update:open="value => !value && closeGatewayInstaller()"
      @agents-changed="loadAgents"
    />

    <!-- ===== 生成安装脚本 ===== -->
    <Dialog v-model:open="scriptDialogVisible">
      <DialogContent class="flex max-w-[760px] flex-col overflow-hidden">
        <DialogHeader class="shrink-0">
          <DialogTitle>生成 Agent 安装脚本</DialogTitle>
          <DialogDescription>选择服务，填写机器信息，再复制命令执行。</DialogDescription>
        </DialogHeader>

        <div class="cf-focus-gutter flex max-h-[65dvh] min-h-0 flex-col gap-5 overflow-y-auto">
          <FormField label="使用模式" hint="普通模式保持原有安装方式；网关模式引导安装 Mihomo、OxiDNS 和两个 Agent。">
            <RadioGroup v-model="installationMode" class="flex flex-wrap gap-2" aria-label="使用模式">
              <label :class="[chipClass(installationMode === 'ordinary'), 'min-h-11']"><RadioGroupItem value="ordinary" />普通模式</label>
              <label :class="[chipClass(installationMode === 'gateway'), 'min-h-11']"><RadioGroupItem value="gateway" />Linux 选择性网关</label>
            </RadioGroup>
          </FormField>
          <div v-if="installationMode === 'gateway'" class="space-y-4 rounded-lg border border-border bg-muted p-4">
            <div class="space-y-2 text-sm leading-relaxed">
              <p class="m-0 font-semibold">从一台 Linux 开始，按步骤装好网关</p>
              <p class="m-0 text-muted-foreground">填写网络信息 → 选择分流规则 → 一条命令安装核心和 Agent → 发布并检查服务 → 应用转发。主路由不需要静态路由。</p>
              <p class="m-0 break-words text-muted-foreground">本次固定使用配置空间「{{ activeProfileName }}」。建议使用专用配置空间，已有设备也会共用此空间的 Mihomo 和 OxiDNS 配置。</p>
            </div>
            <Button class="min-h-11" @click="openGatewayInstaller">开始或继续网关安装</Button>
          </div>
          <template v-else>
          <div class="space-y-4">
            <FormField label="安装类型" :help="installTypeHint">
              <RadioGroup v-model="scriptForm.installType" class="flex gap-2" @update:model-value="onInstallTypeChange">
                <label v-for="option in INSTALL_TYPES" :key="option.value" :class="[chipClass(scriptForm.installType === option.value), 'min-h-11']">
                  <RadioGroupItem :value="option.value" />
                  {{ option.label }}
                </label>
              </RadioGroup>
            </FormField>

            <FormField v-if="scriptForm.installType === 'shell'" label="Shell 模式">
              <RadioGroup v-model="scriptForm.shellMode" class="flex flex-wrap gap-2" @update:model-value="resetForm">
                <label v-for="option in SHELL_MODES" :key="option.value" :class="[chipClass(scriptForm.shellMode === option.value), 'min-h-11']">
                  <RadioGroupItem :value="option.value" />
                  {{ option.label }}
                </label>
              </RadioGroup>
            </FormField>

            <FormField v-if="scriptForm.installType === 'shell' && scriptForm.shellMode === 'single'" label="服务类型">
              <RadioGroup v-model="scriptForm.type" class="grid grid-cols-3 gap-2 sm:flex" @update:model-value="onServiceTypeChange">
                <label v-for="option in serviceTypeOptions" :key="option.value" :class="[chipClass(scriptForm.type === option.value), 'min-h-11 justify-center max-sm:gap-1.5 max-sm:px-2']">
                  <RadioGroupItem :value="option.value" />
                  {{ option.label }}
                </label>
              </RadioGroup>
            </FormField>

            <FormField v-if="scriptForm.installType === 'docker'" label="Docker 模式" :help="dockerModeHint">
              <RadioGroup v-model="scriptForm.dockerMode" class="flex flex-wrap gap-2" @update:model-value="onDockerModeChange">
                <label v-for="option in DOCKER_MODES" :key="option.value" :class="[chipClass(scriptForm.dockerMode === option.value), 'min-h-11']">
                  <RadioGroupItem :value="option.value" />
                  {{ option.label }}
                </label>
              </RadioGroup>
            </FormField>
          </div>

          <div class="grid grid-cols-2 gap-4 border-t border-border/60 pt-4">
            <FormField label="Agent 名称" html-for="agent-name" :class="scriptForm.installType === 'shell' && scriptForm.shellMode === 'single' ? 'min-w-0' : 'col-span-2 sm:col-span-1'">
              <Input id="agent-name" v-model="scriptForm.name" class="min-h-11 bg-background/50" placeholder="例如：home-server" />
            </FormField>

            <FormField v-if="scriptForm.installType === 'shell' && scriptForm.shellMode === 'single'" label="Agent 端口" html-for="agent-port">
              <Input id="agent-port" v-model.number="scriptForm.port" type="number" :min="1024" :max="65535" class="min-h-11 bg-background/50 font-mono" />
            </FormField>

            <FormField label="Agent IP（可选）" html-for="agent-ip" :class="scriptForm.installType === 'shell' && scriptForm.shellMode === 'single' ? 'col-span-2' : 'col-span-2 sm:col-span-1'">
              <Input id="agent-ip" v-model="scriptForm.agent_ip" class="min-h-11 bg-background/50 font-mono" placeholder="留空则自动获取" />
            </FormField>

            <FormField v-if="scriptForm.installType === 'docker'" label="Agent 容器名" html-for="docker-container" class="col-span-2">
              <Input id="docker-container" v-model="scriptForm.containerName" class="min-h-11 bg-background/50 font-mono" placeholder="configflow-agent" />
            </FormField>

            <template v-if="!(scriptForm.installType === 'shell' && scriptForm.shellMode === 'single')">
              <FormField v-if="shellAio || scriptForm.dockerMode === 'mihomo' || scriptForm.dockerMode === 'aio'" label="Mihomo Agent 端口" html-for="mihomo-agent-port">
                <Input id="mihomo-agent-port" v-model.number="scriptForm.mihomoAgentPort" type="number" :min="1024" :max="65535" class="min-h-11 bg-background/50 font-mono" />
              </FormField>
              <FormField v-if="!shellAio && scriptForm.dockerMode === 'mosdns'" label="MosDNS Agent 端口" html-for="mosdns-agent-port">
                <Input id="mosdns-agent-port" v-model.number="scriptForm.mosdnsAgentPort" type="number" :min="1024" :max="65535" class="min-h-11 bg-background/50 font-mono" />
              </FormField>
              <FormField v-if="shellAio || scriptForm.dockerMode === 'oxidns' || scriptForm.dockerMode === 'aio'" label="OxiDNS Agent 端口" html-for="oxidns-agent-port">
                <Input id="oxidns-agent-port" v-model.number="scriptForm.oxidnsAgentPort" type="number" :min="1024" :max="65535" class="min-h-11 bg-background/50 font-mono" />
              </FormField>
            </template>
          </div>

          <div v-if="shellAio || singleCoreSupported" class="min-w-0">
            <label :for="shellAio ? 'aio-install-cores' : 'agent-install-core'" class="flex min-h-11 cursor-pointer items-center gap-2 text-[13px] font-medium">
              <Checkbox :id="shellAio ? 'aio-install-cores' : 'agent-install-core'" v-model="scriptForm.installCore" :aria-describedby="shellAio ? 'aio-install-cores-note' : 'agent-install-core-note'" />
              同时安装 {{ shellAio ? 'Mihomo 与 OxiDNS' : scriptForm.type === 'mihomo' ? 'Mihomo' : 'OxiDNS' }} 核心
            </label>
            <p :id="shellAio ? 'aio-install-cores-note' : 'agent-install-core-note'" class="m-0 pl-6 text-[12px] leading-relaxed text-muted-foreground">
              {{ combinedCoreInstall ? '保留已有核心，仅补装缺少部分；使用默认路径。' : '未勾选时仅安装 Agent，接管已有核心。' }}
            </p>
          </div>

          <div class="divide-y divide-border/60 border-y border-border/60">
            <Collapsible v-if="shellAio" v-model:open="lifecyclePanelOpen">
              <CollapsibleTrigger aria-label="核心服务高级配置" class="flex min-h-11 w-full items-center gap-2 py-3 text-left text-[13px] font-medium">
                核心服务高级配置
                <span class="ml-auto text-[12px] font-normal text-muted-foreground">路径 / 服务</span>
                <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="lifecyclePanelOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent class="space-y-4 pb-4">
                <p v-if="aioCoreInstall" class="m-0 text-[12px] text-muted-foreground">同时安装核心时使用默认路径；接管自定义部署请取消勾选。</p>
                <div class="grid gap-4 sm:grid-cols-2">
                  <FormField label="Mihomo 配置文件路径" html-for="aio-mihomo-config-path">
                    <Input id="aio-mihomo-config-path" :model-value="aioServiceSettings.mihomoConfigPath" :disabled="aioCoreInstall" @update:model-value="scriptForm.mihomoConfigPath = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="/etc/mihomo/config.yaml" />
                  </FormField>
                  <FormField label="OxiDNS 配置文件路径" html-for="aio-oxidns-config-path">
                    <Input id="aio-oxidns-config-path" :model-value="aioServiceSettings.oxidnsConfigPath" :disabled="aioCoreInstall" @update:model-value="scriptForm.oxidnsConfigPath = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="/etc/oxidns/config.yaml" />
                  </FormField>
                  <FormField label="Mihomo 服务名称" html-for="aio-mihomo-service-unit">
                    <Input id="aio-mihomo-service-unit" :model-value="aioServiceSettings.mihomoServiceUnit" :disabled="aioCoreInstall" @update:model-value="scriptForm.mihomoServiceUnit = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="mihomo" />
                  </FormField>
                  <FormField label="OxiDNS 服务名称" html-for="aio-oxidns-service-unit">
                    <Input id="aio-oxidns-service-unit" :model-value="aioServiceSettings.oxidnsServiceUnit" :disabled="aioCoreInstall" @update:model-value="scriptForm.oxidnsServiceUnit = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="oxidns" />
                  </FormField>
                  <FormField label="Mihomo 内核程序路径" html-for="aio-mihomo-service-binary" help="填写现有服务使用的程序名或绝对路径。">
                    <Input id="aio-mihomo-service-binary" :model-value="aioServiceSettings.mihomoServiceBinary" :disabled="aioCoreInstall" @update:model-value="scriptForm.mihomoServiceBinary = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="mihomo" />
                  </FormField>
                  <FormField label="OxiDNS 内核程序路径" html-for="aio-oxidns-service-binary" help="填写现有服务使用的程序名或绝对路径。">
                    <Input id="aio-oxidns-service-binary" :model-value="aioServiceSettings.oxidnsServiceBinary" :disabled="aioCoreInstall" @update:model-value="scriptForm.oxidnsServiceBinary = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="oxidns" />
                  </FormField>
                </div>
                <p v-if="!aioCoreInstall" class="m-0 text-[12px] leading-relaxed text-muted-foreground">OxiDNS 服务的 <code class="font-mono">-d</code> 工作目录须与配置文件所在目录一致。</p>
              </CollapsibleContent>
            </Collapsible>

            <Collapsible v-if="scriptForm.installType === 'shell' && scriptForm.shellMode === 'single'" v-model:open="lifecyclePanelOpen">
              <CollapsibleTrigger aria-label="服务管理高级配置（可选）" class="flex min-h-11 w-full items-center gap-2 py-3 text-left text-[13px] font-medium">
                服务管理高级配置（可选）
                <span class="ml-auto whitespace-nowrap text-[12px] font-normal text-muted-foreground">路径 / 服务</span>
                <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="lifecyclePanelOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent class="space-y-4 pb-4">
                <p v-if="singleCoreInstall" class="m-0 text-[12px] text-muted-foreground">同时安装核心时使用默认路径；接管自定义部署请取消勾选。</p>
                <FormField label="配置文件路径" html-for="agent-config-path" help="填写目标机器上的完整配置路径，须包含文件名。">
                  <Input id="agent-config-path" :model-value="singleServiceSettings.config_path" :disabled="singleCoreInstall" @update:model-value="scriptForm.config_path = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="/etc/mihomo/config.yaml" />
                </FormField>
                <p v-if="scriptForm.type === 'oxidns' && !singleCoreInstall" class="m-0 text-[12px] leading-relaxed text-muted-foreground">OxiDNS 的 <code class="font-mono">-d</code> 工作目录须与配置文件所在目录一致；分离布局暂不支持，详见安装说明。</p>
                <FormField label="重启命令（可选）" html-for="agent-restart" help="留空自动识别 systemd / OpenRC；自定义命令需同时提供停止、启动及状态检查命令。">
                  <Input id="agent-restart" :model-value="singleServiceSettings.restart_command" :disabled="singleCoreInstall" @update:model-value="scriptForm.restart_command = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" placeholder="留空自动识别" />
                </FormField>
                <div class="grid gap-4 sm:grid-cols-2">
                  <FormField label="服务管理方式">
                    <Select :model-value="singleServiceSettings.service_manager" :disabled="singleCoreInstall" @update:model-value="scriptForm.service_manager = String($event ?? '')">
                      <SelectTrigger class="min-h-11 w-full bg-background/50"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="auto">自动识别 systemd / OpenRC</SelectItem>
                        <SelectItem value="systemd">systemd</SelectItem>
                        <SelectItem value="openrc">OpenRC</SelectItem>
                        <SelectItem value="command">自定义命令</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField label="服务名称" html-for="agent-service-unit" help="留空使用核心名称，也可填写自定义 systemd 单元或 OpenRC 服务名。">
                    <Input id="agent-service-unit" :model-value="singleServiceSettings.service_unit" :disabled="singleCoreInstall" @update:model-value="scriptForm.service_unit = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" :placeholder="scriptForm.type" />
                  </FormField>
                </div>
                <FormField label="内核程序路径" html-for="agent-service-binary" help="应指向现有服务使用的程序；留空按程序名查找。">
                  <Input id="agent-service-binary" :model-value="singleServiceSettings.service_binary" :disabled="singleCoreInstall" @update:model-value="scriptForm.service_binary = String($event ?? '')" class="min-h-11 bg-background/50 font-mono" :placeholder="`/usr/local/bin/${scriptForm.type}`" />
                </FormField>
                <template v-if="!singleCoreInstall && (scriptForm.service_manager === 'command' || scriptForm.restart_command.trim())">
                  <FormField label="停止命令" html-for="agent-service-stop" help="应等待服务完全停止后再退出。">
                    <Input id="agent-service-stop" v-model="scriptForm.stop_command" class="min-h-11 bg-background/50 font-mono" placeholder="例如：/opt/service/control stop" />
                  </FormField>
                  <FormField label="启动命令" html-for="agent-service-start">
                    <Input id="agent-service-start" v-model="scriptForm.start_command" class="min-h-11 bg-background/50 font-mono" placeholder="例如：/opt/service/control start" />
                  </FormField>
                  <FormField label="状态检查命令" html-for="agent-service-status" help="服务运行时退出码为 0，未运行时为非 0。">
                    <Input id="agent-service-status" v-model="scriptForm.status_command" class="min-h-11 bg-background/50 font-mono" placeholder="例如：/opt/service/control status" />
                  </FormField>
                </template>
              </CollapsibleContent>
            </Collapsible>

            <Collapsible v-if="scriptForm.installType === 'docker'" v-model:open="dockerOptionsOpen">
              <CollapsibleTrigger class="flex min-h-11 w-full items-center gap-2 py-3 text-left text-[13px] font-medium">
                Docker 高级配置
                <span class="ml-auto text-[12px] font-normal text-muted-foreground">镜像 / 网络</span>
                <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="dockerOptionsOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent class="space-y-4 pb-4">
                <FormField label="Docker 镜像" html-for="docker-image">
                  <Input id="docker-image" v-model="scriptForm.dockerImage" class="min-h-11 bg-background/50 font-mono" placeholder="留空使用官方镜像" />
                </FormField>
                <FormField label="网络模式" help="host 直接访问主机网络；bridge 通过端口映射访问。">
                  <RadioGroup v-model="scriptForm.networkMode" class="flex flex-wrap gap-2">
                    <label v-for="option in networkModeOptions" :key="option.value" :class="[chipClass(scriptForm.networkMode === option.value), 'min-h-11']">
                      <RadioGroupItem :value="option.value" />
                      {{ option.label }}
                    </label>
                  </RadioGroup>
                </FormField>
              </CollapsibleContent>
            </Collapsible>

            <Collapsible v-model:open="installHelpOpen">
              <CollapsibleTrigger aria-label="安装说明" class="flex min-h-11 w-full items-center gap-2 py-3 text-left text-[13px] font-medium">
                安装说明
                <span class="ml-auto text-[12px] font-normal text-muted-foreground">{{ scriptForm.installType === 'docker' ? '部署须知' : 'DNS / TUN' }}</span>
                <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="installHelpOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent class="space-y-3 pb-4 text-[12.5px] leading-relaxed text-muted-foreground">
                <p class="m-0">Agent 名称可使用字母、数字、短横线或下划线。生成命令不会注册 Agent，需在目标机器执行后才会接入面板。</p>
                <template v-if="scriptForm.installType === 'shell'">
                  <p class="m-0">在目标机器以 root 身份执行，支持 systemd / OpenRC。勾选安装核心后，脚本先安装核心，成功后再安装 Agent；已有核心和配置会保留，仅补装缺少的核心。</p>
                  <p class="m-0">默认使用 curl，也可切换 wget；完整下载为当前目录的 configflow-install.sh 后才执行。环境检测与依赖补装由脚本完成；两个下载工具都没有时，请先安装一个。</p>
                  <p class="m-0 break-words">安装核心时使用 <code class="font-mono">/etc/{{ shellAio ? '{mihomo,oxidns}' : scriptForm.type }}/config.yaml</code>，程序位于 <code class="font-mono">/usr/local/bin</code>，服务名与核心同名。已有部署须与默认布局一致；自定义部署请取消勾选，并在高级配置中填写实际路径。</p>
                  <template v-if="shellAio">
                    <p class="m-0"><strong class="font-medium text-foreground">初始 DNS：</strong>OxiDNS <code class="font-mono">53</code> → Mihomo <code class="font-mono">127.0.0.1:1053</code>。发布时保留自定义监听地址、端口及 <code class="font-mono">dns-hijack</code>，并检查实际 DNS 配置。</p>
                    <p v-if="aioCoreInstall" class="m-0">新装 OxiDNS 会自动使用上述上游；已有配置保持不变，请确认已有核心的 DNS 监听与上游设置。</p>
                    <p v-else class="m-0">请先准备两个核心及其 systemd / OpenRC 服务，并将 OxiDNS 的远端 DNS 上游设为 Mihomo 实际的 DNS 监听地址和端口。</p>
                  </template>
                  <p v-else-if="singleCoreInstall && scriptForm.type === 'mihomo'" class="m-0"><strong class="font-medium text-foreground">Mihomo DNS：</strong>监听 <code class="font-mono">1053</code>，使用公共 DNS 上游。</p>
                  <p v-else-if="singleCoreInstall && scriptForm.type === 'oxidns'" class="m-0"><strong class="font-medium text-foreground">OxiDNS：</strong>监听 <code class="font-mono">53</code>，使用公共 DNS 上游，独立运行且不依赖 Mihomo。接入后可在面板修改上游。</p>
                  <p v-if="shellAio || scriptForm.type === 'mihomo'" class="m-0"><strong class="font-medium text-foreground">TUN：</strong>首次安装 Mihomo 时检测设备与权限，可用则开启 TUN 和自动路由，否则关闭并提示修复方法。初始流量直连，尚无代理节点；作为网关还需开启 IPv4 转发，脚本仅提示、不修改。发布前请检查面板草稿的 TUN 开关。</p>
                  <p v-if="(shellAio || scriptForm.type === 'oxidns') && !combinedCoreInstall" class="m-0"><strong class="font-medium text-foreground">OxiDNS 路径：</strong>服务的 <code class="font-mono">-d</code> 工作目录须与配置目录一致，例如 <code class="font-mono">/opt/oxidns/config.yaml</code> 对应 <code class="font-mono">-d /opt/oxidns</code>。官方 Debian 的 <code class="font-mono">/etc</code> 配置与 <code class="font-mono">/var/lib</code> 工作目录分离布局暂不支持，需先调整服务与规则资源布局。</p>
                </template>
                <template v-else>
                  <p class="m-0 font-medium text-foreground">{{ dockerNote.title }}</p>
                  <ul class="m-0 list-disc space-y-1 pl-4"><li v-for="line in dockerNote.lines" :key="line">{{ line }}</li></ul>
                </template>
              </CollapsibleContent>
            </Collapsible>

            <Collapsible v-if="shellAio && !aioCoreInstall" v-model:open="coreOnlyPanelOpen">
              <CollapsibleTrigger id="core-install-title" aria-label="仅安装核心" class="flex min-h-11 w-full items-center gap-2 py-3 text-left text-[13px] font-medium">
                仅安装核心
                <span class="ml-auto text-[12px] font-normal text-muted-foreground">不安装 Agent</span>
                <ChevronDown class="size-3.5 shrink-0 transition-transform" :class="coreOnlyPanelOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <section aria-labelledby="core-install-title" class="min-w-0 space-y-3 pb-4">
                  <p class="m-0 text-[12.5px] text-muted-foreground">无需填写 Agent 信息。保留已有核心和配置，仅补装缺少部分。</p>
                  <RadioGroup v-model="coreInstallTarget" aria-label="要安装的核心" class="flex flex-wrap gap-2">
                    <label v-for="option in CORE_INSTALL_TARGETS" :key="option.value" :class="[chipClass(coreInstallTarget === option.value), 'min-h-11']">
                      <RadioGroupItem :value="option.value" />
                      {{ option.label }}
                    </label>
                  </RadioGroup>
                  <p v-if="coreInstallTarget === 'both'" class="m-0 text-[12px] leading-relaxed text-muted-foreground">OxiDNS 53 → Mihomo 1053。已有 Mihomo 时仅补装 OxiDNS；请确认 Mihomo DNS 监听 <code class="font-mono">127.0.0.1:1053</code>。</p>
                  <p v-else-if="coreInstallTarget === 'oxidns'" class="m-0 text-[12px] leading-relaxed text-muted-foreground">OxiDNS 监听 53，使用公共 DNS 上游，无需 Mihomo。</p>
                  <p v-else class="m-0 text-[12px] leading-relaxed text-muted-foreground">Mihomo DNS 监听 1053，使用公共 DNS 上游。</p>
                  <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">以 root 执行，支持 systemd / OpenRC；自动补齐依赖，完整下载后安装。使用默认路径，不受高级配置影响。TUN 检测规则见安装说明。</p>
                  <template v-if="coreInstallCommand">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <RadioGroup v-model="installerDownloader" aria-label="核心安装命令下载方式" class="flex gap-2">
                        <label v-for="downloader in INSTALLER_DOWNLOADERS" :key="downloader" :class="[chipClass(installerDownloader === downloader), 'min-h-11']">
                          <RadioGroupItem :value="downloader" />{{ downloader }}
                        </label>
                      </RadioGroup>
                      <Button variant="outline" class="min-h-11" @click="copyAgentContent(coreInstallCommand, '核心安装命令已复制到剪贴板')">
                        <Copy class="size-4" />复制命令
                      </Button>
                    </div>
                    <p class="m-0 text-[11.5px] text-muted-foreground">链接有效至 {{ coreInstallLinkExpiry }}，过期后请重新生成。</p>
                    <Textarea id="core-install-command" :model-value="coreInstallCommand" :aria-label="`${coreInstallLabel}的独立安装命令`" readonly :rows="3" class="max-h-48 bg-background/50 font-mono text-[11.5px]" />
                    <Collapsible>
                      <CollapsibleTrigger class="min-h-11 w-full text-left text-[12.5px] text-muted-foreground">查看核心安装脚本</CollapsibleTrigger>
                      <CollapsibleContent>
                        <p class="mt-0 text-[12px] text-muted-foreground">已包含所选核心，与上方链接下载的脚本一致。</p>
                        <Textarea :model-value="coreInstallScript" :aria-label="`${coreInstallLabel}的独立安装脚本`" readonly :rows="12" class="max-h-64 bg-background/50 font-mono text-[11.5px]" />
                      </CollapsibleContent>
                    </Collapsible>
                  </template>
                  <Button v-else variant="outline" class="min-h-11" :disabled="coreInstallLoading" @click="generateCoreInstallCommand">
                    <Loader2 v-if="coreInstallLoading" class="size-4 animate-spin" />
                    <Download v-else class="size-4" />
                    {{ coreInstallLoading ? '正在生成核心安装命令…' : `生成${coreInstallLabel}的安装命令` }}
                  </Button>
                </section>
              </CollapsibleContent>
            </Collapsible>
          </div>

          <section v-if="installCommand || dockerComposeContent || dockerRunCommand" ref="installCommandSection" aria-label="一键安装命令" class="space-y-4">
            <LabeledDivider label="一键安装命令" />
            <RadioGroup v-if="scriptForm.installType === 'shell'" v-model="installerDownloader" aria-label="安装命令下载方式" class="flex gap-2">
              <label v-for="downloader in INSTALLER_DOWNLOADERS" :key="downloader" :class="[chipClass(installerDownloader === downloader), 'min-h-11']">
                <RadioGroupItem :value="downloader" />{{ downloader }}
              </label>
            </RadioGroup>
            <p v-if="scriptForm.installType === 'shell'" class="m-0 text-[11.5px] text-muted-foreground">链接有效至 {{ installLinkExpiry }}，过期后请重新生成。</p>
            <div v-for="block in commandBlocks" :key="block.title" class="flex min-w-0 flex-col gap-2">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="text-[12.5px] font-semibold text-foreground">{{ block.title }}</span>
                <Button variant="outline" class="min-h-11 border-border/60 bg-background/40" @click="block.copy"><Copy class="size-4" />复制</Button>
              </div>
              <p class="m-0 text-[11.5px] text-muted-foreground">{{ block.note }}</p>
              <Textarea :model-value="block.value" readonly :rows="block.rows" class="max-h-48 bg-background/50 font-mono text-[11.5px]" />
            </div>
            <Collapsible v-model:open="scriptPanelOpen">
              <CollapsibleTrigger class="flex min-h-11 w-full cursor-pointer items-center gap-2 py-2 text-left text-[13px] text-muted-foreground">
                查看完整安装脚本
                <ChevronDown class="ml-auto size-3.5 transition-transform duration-200" :class="scriptPanelOpen && 'rotate-180'" />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <Textarea :model-value="installScript" readonly :rows="15" class="mt-2 max-h-72 bg-background/50 font-mono text-[11.5px]" />
              </CollapsibleContent>
            </Collapsible>
          </section>
          </template>
        </div>

        <DialogFooter class="shrink-0 flex-row items-center justify-end gap-2 border-t border-border/60 pt-4">
          <Button variant="outline" class="min-h-11" @click="scriptDialogVisible = false">关闭</Button>
          <template v-if="installationMode === 'ordinary'">
          <Button v-if="installCommand || dockerComposeContent || dockerRunCommand" variant="outline" class="min-h-11" @click="resetForm">重新生成</Button>
          <Button v-else class="min-h-11" :disabled="scriptLoading" @click="generateScript">
            <Loader2 v-if="scriptLoading" class="size-4 animate-spin" />
            <FileText v-else class="size-4" />
            {{ scriptLoading ? '正在生成安装命令…' : scriptForm.installType === 'docker' ? '生成 Docker 部署命令' : '生成安装命令' }}
          </Button>
          </template>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- ===== Agent 日志 ===== -->
    <Dialog v-model:open="logsDialogVisible">
      <DialogContent class="max-w-[900px]">
        <DialogHeader>
          <DialogTitle>Agent 日志 · {{ currentAgent?.name }}</DialogTitle>
          <DialogDescription>选择此 Agent 机器上的日志文件查看内容。清空会删除所选文件中的全部日志，且无法恢复。</DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-3">
          <div class="flex flex-wrap items-center gap-2">
            <Select v-model="selectedLogPath" @update:model-value="value => onLogPathChange(String(value))">
              <SelectTrigger class="h-9 w-[260px] bg-background/50 text-[13px]">
                <SelectValue placeholder="选择日志文件" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="opt in logPathOptions" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </SelectItem>
                <SelectItem value="custom">自定义路径…</SelectItem>
              </SelectContent>
            </Select>

            <template v-if="selectedLogPath === 'custom'">
              <Input
                v-model="customLogPath"
                class="w-[300px] bg-background/50 font-mono"
                placeholder="/var/log/your-file.log"
                @keyup.enter="validateAndLoadCustomPath"
              />
              <Button
                variant="outline"
                class="border-border/60 bg-background/40"
                :disabled="validatingPath"
                @click="validateAndLoadCustomPath"
              >
                <Loader2 v-if="validatingPath" class="size-4 animate-spin" />
                查看日志
              </Button>
            </template>
          </div>

          <pre
            ref="logsPaneRef"
            class="m-0 max-h-[46dvh] overflow-auto rounded-lg border border-border/50 bg-background/45 p-3 font-mono text-[11.5px] leading-[1.7] whitespace-pre-wrap text-foreground/85"
          >{{ logs || '暂无日志' }}</pre>
        </div>

        <DialogFooter class="sm:justify-between">
          <label
            v-if="isMainAgentLog"
            class="flex items-center gap-2 text-[12.5px] text-muted-foreground"
          >
            <Switch
              :model-value="loggingEnabled"
              :disabled="togglingLogging"
              @update:model-value="value => toggleLogging(Boolean(value))"
            />
            {{ loggingEnabled ? '日志已启用' : '日志已禁用' }}
          </label>
          <div class="flex items-center gap-2">
            <Button variant="outline" @click="logsDialogVisible = false">关闭</Button>
            <Button
              variant="outline"
              class="border-destructive-accent/30 bg-destructive-soft/40 text-destructive-accent"
              @click="clearLogs"
            >
              <Trash2 class="size-4" />
              清空
            </Button>
            <Button @click="refreshLogs">
              <RefreshCw class="size-4" />
              刷新
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- ===== 监控详情 ===== -->
    <Dialog v-model:open="metricsDialogVisible">
      <DialogContent class="max-w-[1100px]">
        <DialogHeader>
          <DialogTitle>系统监控 · {{ currentMetricsAgent?.name }}</DialogTitle>
          <DialogDescription>查看最近 24 小时的资源使用情况，已有 {{ metricsHistory.length }} 条记录。</DialogDescription>
        </DialogHeader>

        <LoadingRows v-if="metricsLoading" :rows="4" />

        <EmptyState
          v-else-if="metricsHistory.length === 0"
          :icon="ChartLine"
          title="暂无监控数据"
          description="等待 Agent 连接并发送运行数据后，点击「刷新」查看。"
        />

        <div v-else class="flex max-h-[68dvh] flex-col gap-3 overflow-y-auto pr-1">
          <div
            v-if="currentMetricsAgent?.system_metrics"
            class="grid grid-cols-5 gap-2 max-[900px]:grid-cols-2"
          >
            <div
              v-for="item in metricsSummary"
              :key="item.label"
              class="rounded-lg border border-border/50 bg-background/40 p-3"
            >
              <p class="m-0 text-[11px] text-muted-foreground">{{ item.label }}</p>
              <p class="num mt-1 mb-0 text-[15px] font-semibold" :style="{ color: item.color }">
                {{ item.value }}
              </p>
              <p v-if="item.detail" class="num mt-0.5 mb-0 text-[11px] text-muted-foreground">
                {{ item.detail }}
              </p>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3 max-[900px]:grid-cols-1">
            <div
              v-for="chart in metricsCharts"
              :key="chart.key"
              class="rounded-xl border border-border/50 bg-background/40 p-2"
            >
              <v-chart :option="chart.option" :autoresize="true" style="height: 280px" />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" @click="metricsDialogVisible = false">关闭</Button>
          <Button @click="showMetricsDetail(currentMetricsAgent)">
            <RefreshCw class="size-4" />
            刷新
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { useChartPalette, withAlpha } from '@/lib/chartPalette'
import PageHeader from '@/components/common/PageHeader.vue'
import { ref, shallowRef, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMediaQuery } from '@vueuse/core'
import BrandMark from '@/components/common/BrandMark.vue'
import FlowMap from '@/components/dashboard/FlowMapViewer.vue'
import type { FlowColumn, FlowEdge, FlowTone } from '@/components/dashboard/FlowMap.vue'
import { consumeAction } from '@/lib/actions'
import { cn } from '@/lib/utils'
import { getAgentConfigSync } from '@/lib/agentConfigSync'
import { buildInstallCommand, AGENT_CORE_INSTALL_INTERFACE, CORE_INSTALL_INTERFACE, type CoreInstallTarget, type InstallerDownloader } from '@/lib/coreInstallCommand'
import { supportsAgentSettings, type AgentSettingsContext } from '@/lib/agentSettings'
import DockerAgentUpdateDialog from '@/components/agents/DockerAgentUpdateDialog.vue'
import AgentTraffic from '@/components/agents/AgentTraffic.vue'
import GatewayInstallWizard from '@/components/agents/GatewayInstallWizard.vue'
import { Motion } from 'motion-v'
import {
  ChartLine,
  ChevronDown,
  CircleCheck,
  Copy,
  Download,
  FileText,
  History,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  RotateCw,
  ScrollText,
  Send,
  Server,
  Settings,
  Trash2,
  TriangleAlert,
  Upload,
  X
} from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import EmptyState from '@/components/common/EmptyState.vue'
import FormField from '@/components/common/FormField.vue'
import LabeledDivider from '@/components/common/LabeledDivider.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import StatTile from '@/components/common/StatTile.vue'
import StatusDot from '@/components/common/StatusDot.vue'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import { copyText } from '@/lib/clipboard'
import { listItem } from '@/lib/motion'
import { agentApi, type AgentInstallScriptParams } from '@/api'
import api from '@/api'
import type { Agent } from '@/types'
import { useProfileStore } from '@/stores/profile'
import { createDeploymentId, deploymentNeedsNotice, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import AgentDeploymentHistory from '@/components/agents/AgentDeploymentHistory.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentDNSFollow from '@/components/agents/AgentDNSFollow.vue'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import AgentUpgradeStatus from '@/components/agents/AgentUpgradeStatus.vue'
import OxiDNSRuleUpdate from '@/components/agents/OxiDNSRuleUpdate.vue'
import AgentRuleSync from '@/components/agents/AgentRuleSync.vue'
import { submitAgentRequests } from '@/lib/agentRequests'
import { deploymentErrorSummary } from '@/lib/agentFailure'
import { upgradeLabels, useAgentUpgrades } from '@/composables/useAgentUpgrades'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, TitleComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'

// Register ECharts components
use([LineChart, GridComponent, TooltipComponent, LegendComponent, TitleComponent, CanvasRenderer])


const agents = ref<Agent[]>([])
const { deployments, busy: deploymentBusy, track: trackDeployment, beginSubmission, endSubmission, refresh: refreshDeployment, activate: activateDeployment, retryPublish: retryDeployment, rollback: rollbackDeployment } = useAgentDeployments(() => { void loadAgents() })
const deploymentHistoryAgentId = ref<string | null>(null)
const deploymentHistoryAgent = computed(() => agents.value.find(agent => agent.id === deploymentHistoryAgentId.value))
const rollbackConfirming = ref(false)
const requestRollback = async (agent: Agent, record: AgentDeployment) => {
  if (rollbackConfirming.value || agent.status !== 'online' || deploymentBusy(agent.id) || upgradeBusy(agent.id) || pushingIds.value.has(agent.id)) return
  if (agent.latest_deployment?.deployment_id !== record.deployment_id || record.status !== 'succeeded') return
  rollbackConfirming.value = true
  try {
    const accepted = await confirm(`将 ${agent.name} 恢复到发布 ${record.deployment_id} 之前的配置和受管规则，并重启核心检查健康状态。state/ 运行数据保留。`, {
      title: '回滚到发布前的配置？', confirmText: '回滚并重启', cancelText: '取消'
    })
    if (!accepted) return
    const current = agents.value.find(item => item.id === agent.id)
    if (!current || current.status !== 'online' || deploymentBusy(agent.id) || upgradeBusy(agent.id) || pushingIds.value.has(agent.id) || current.latest_deployment?.deployment_id !== record.deployment_id) return
    await rollbackDeployment(agent.id, record.deployment_id)
  } finally {
    rollbackConfirming.value = false
  }
}
const {
  upgrades, busy: upgradeBusy, querying: upgradeQuerying, track: trackUpgrade, query: queryAgentUpgrade, start: startUpgrade, retryUnknown: retryUnknownUpgrade
} = useAgentUpgrades(() => { void loadAgents() })

const retryUnconfirmedUpgrade = async (agentId: string) => {
  try {
    await retryUnknownUpgrade(agentId, () => confirm('尚未发现本次更新的新任务记录。重新提交前会再次由 Agent 检查是否已有更新在执行；如已有任务，系统会阻止重复执行，请查看最新结果。', {
      title: '重新提交更新请求？', confirmText: '重新提交', cancelText: '继续等待'
    }))
  } catch (error: any) { notify.error('暂时无法重新提交', error.response?.data?.message || error.message || '请稍后查询更新结果。') }
}

const queryUpgrade = async (agentId: string) => {
  try {
    const state = await queryAgentUpgrade(agentId)
    if (!state) return
    const label = upgradeLabels[state.status] || state.status
    if (state.status === 'succeeded') notify.success(label)
    else if (['failed', 'rolled_back', 'rollback_failed'].includes(state.status)) notify.error(state.error || label)
    else notify.info(`当前状态：${label}`)
  } catch (error: any) {
    notify.error(error.response?.data?.message || '查询更新结果失败，请稍后重试')
  }
}
const { profiles, refreshProfiles, defaultProfileId } = useProfileStore()
const bindingAgentId = ref<string | null>(null)
const agentSettings = ref<AgentSettingsContext | null>(null)
const agentSettingsEditor = shallowRef<typeof import('./Generate.vue')['default']>()
const settingsLoadingAgentId = ref<string | null>(null)
let settingsPageActive = true

const openAgentSettings = async (agent: Agent) => {
  if (settingsLoadingAgentId.value || agentSettings.value || bindingAgentId.value === agent.id || !supportsAgentSettings(agent.service_type)) return
  settingsLoadingAgentId.value = agent.id
  try {
    // Refresh both sources before capturing the scope: another tab may have
    // rebound the device or changed the default for an older unbound Agent.
    const [response, currentProfiles, editor] = await Promise.all([
      agentApi.getAll(), refreshProfiles(), import('./Generate.vue')
    ])
    if (!settingsPageActive) return
    const current = (response.data as Agent[]).find(item => item.id === agent.id)
    if (!current) throw new Error('此 Agent 已不存在，请刷新列表。')
    if (!supportsAgentSettings(current.service_type)) throw new Error('此 Agent 的软件类型暂不支持配置设置。')
    const profileId = current.profile_id || defaultProfileId.value
    const profile = currentProfiles.find(item => item.id === profileId)
    if (!profile) throw new Error('此 Agent 绑定的配置空间已不存在，请重新绑定后再设置。')
    agentSettingsEditor.value = editor.default
    agentSettings.value = {
      agentId: current.id, agentName: current.name, serviceType: current.service_type,
      profileId: profile.id, profileName: profile.name,
    }
  } catch (error: any) {
    if (settingsPageActive) {
      settingsLoadingAgentId.value = null
      notify.error('打开配置设置失败', error.response?.data?.message || error.message || '请稍后重试。')
    }
  }
}

const closeAgentSettings = () => {
  agentSettings.value = null
  settingsLoadingAgentId.value = null
  void loadAgents()
  void refreshProfiles().catch(() => undefined)
}
const scriptDialogVisible = ref(false)
const installationMode = ref<'ordinary' | 'gateway'>('ordinary')
const gatewayInstallContext = ref<{ profileId: string; profileName: string } | null>(null)
const openGatewayInstaller = () => {
  gatewayInstallContext.value = { profileId: profileStore.activeProfileId.value, profileName: activeProfileName.value }
  scriptDialogVisible.value = false
}
const closeGatewayInstaller = () => {
  gatewayInstallContext.value = null
  void loadAgents()
}
const dockerUpdateAgent = ref<Agent | null>(null)
const lifecyclePanelOpen = ref(false)
const installHelpOpen = ref(false)
const coreOnlyPanelOpen = ref(false)
const dockerOptionsOpen = ref(false)
const logsDialogVisible = ref(false)
const metricsDialogVisible = ref(false)
const installCommandSection = ref<HTMLElement | null>(null)
const installScript = ref('')
const scriptLoading = ref(false)
let scriptRequest = 0
const INSTALLER_DOWNLOADERS = ['curl', 'wget'] as const
const installerDownloader = ref<InstallerDownloader>('curl')
const installScriptUrl = ref('')
const installLinkExpiry = ref('')
const installCommand = computed(() => installScriptUrl.value
  ? buildInstallCommand(installScriptUrl.value, installerDownloader.value)
  : '')
const coreInstallScriptUrl = ref('')
const coreInstallLinkExpiry = ref('')
const CORE_INSTALL_TARGETS = [
  { value: 'both', label: 'Mihomo + OxiDNS' },
  { value: 'mihomo', label: '仅 Mihomo' },
  { value: 'oxidns', label: '仅 OxiDNS' },
] as const
const coreInstallTarget = ref<CoreInstallTarget>('both')
const coreInstallCommand = computed(() => coreInstallScriptUrl.value
  ? buildInstallCommand(coreInstallScriptUrl.value, installerDownloader.value)
  : '')
const coreInstallLabel = computed(() => coreInstallTarget.value === 'both' ? '两个核心' : coreInstallTarget.value === 'mihomo' ? 'Mihomo' : 'OxiDNS')
const coreInstallScript = ref('')
const coreInstallLoading = ref(false)
let coreInstallRequest = 0
const dockerComposeContent = ref('')
const dockerRunCommand = ref('')
const logs = ref('')
const selectedLogPath = ref('/var/log/configflow-agent.log')
const customLogPath = ref('')
const loggingEnabled = ref(true)
const togglingLogging = ref(false)
const validatingPath = ref(false)
const currentAgent = ref<Agent | null>(null)
const currentMetricsAgent = ref<Agent | null>(null)
const metricsHistory = ref<any[]>([])
const metricsLoading = ref(false)
const logsPaneRef = ref<HTMLElement | null>(null)

const createScriptForm = () => ({
  installType: 'shell',
  shellMode: 'single',
  installCore: false,
  dockerMode: 'mihomo', // mihomo, mosdns, oxidns, aio
  name: '',
  type: 'mihomo',
  port: 8080,
  agent_ip: '',
  config_path: '/etc/mihomo/config.yaml',
  restart_command: '',
  service_manager: 'auto',
  service_unit: '',
  service_binary: '',
  stop_command: '',
  start_command: '',
  status_command: '',
  dockerImage: '',
  containerName: 'configflow-agent',
  serviceContainerName: '',
  networkMode: 'bridge',
  // 多服务 Agent 端口配置
  mihomoAgentPort: 8080,
  mosdnsAgentPort: 8081,
  oxidnsAgentPort: 8082,
  mihomoConfigPath: '/etc/mihomo/config.yaml',
  oxidnsConfigPath: '/etc/oxidns/config.yaml',
  mihomoServiceUnit: 'mihomo',
  oxidnsServiceUnit: 'oxidns',
  mihomoServiceBinary: 'mihomo',
  oxidnsServiceBinary: 'oxidns'
})
const scriptForm = ref(createScriptForm())
const shellAio = computed(() => scriptForm.value.installType === 'shell' && scriptForm.value.shellMode === 'aio')
const singleCoreSupported = computed(() => scriptForm.value.installType === 'shell' && scriptForm.value.shellMode === 'single' && ['mihomo', 'oxidns'].includes(scriptForm.value.type))
const singleCoreInstall = computed(() => singleCoreSupported.value && scriptForm.value.installCore)
const aioCoreInstall = computed(() => shellAio.value && scriptForm.value.installCore)
const combinedCoreInstall = computed(() => singleCoreInstall.value || aioCoreInstall.value)
// The displayed defaults do not overwrite the user's existing-service settings.
const singleServiceSettings = computed(() => singleCoreInstall.value ? {
  ...scriptForm.value,
  config_path: `/etc/${scriptForm.value.type}/config.yaml`, restart_command: '',
  service_manager: 'auto', service_unit: scriptForm.value.type,
  service_binary: `/usr/local/bin/${scriptForm.value.type}`,
  stop_command: '', start_command: '', status_command: ''
} : scriptForm.value)
const aioServiceSettings = computed(() => aioCoreInstall.value ? {
  ...scriptForm.value,
  mihomoConfigPath: '/etc/mihomo/config.yaml', oxidnsConfigPath: '/etc/oxidns/config.yaml',
  mihomoServiceUnit: 'mihomo', oxidnsServiceUnit: 'oxidns',
  mihomoServiceBinary: '/usr/local/bin/mihomo', oxidnsServiceBinary: '/usr/local/bin/oxidns'
} : scriptForm.value)
const shellServiceSettings = computed(() => shellAio.value ? aioServiceSettings.value : singleServiceSettings.value)
watch(() => [scriptForm.value.type, scriptForm.value.installType, scriptForm.value.shellMode], () => {
  scriptForm.value.installCore = false
}, { flush: 'sync' })

const resetCoreInstallCommand = () => {
  coreInstallRequest += 1
  coreInstallScriptUrl.value = ''
  coreInstallLinkExpiry.value = ''
  coreInstallScript.value = ''
  coreInstallLoading.value = false
}

watch([shellAio, aioCoreInstall, scriptDialogVisible, coreInstallTarget], resetCoreInstallCommand, { flush: 'sync' })

const installCommandUpdateMessage = '当前服务不支持短安装链接，请更新 ConfigFlow 后重试'

const readInstallCommand = (data: unknown, baseUrl: URL) => {
  if (!data || typeof data !== 'object') throw new Error(installCommandUpdateMessage)
  const { script, download_path: path, expires_at: expiresAt } = data as Record<string, unknown>
  if (typeof script !== 'string' || !script.startsWith('#!')) throw new Error(installCommandUpdateMessage)
  if (typeof path !== 'string' || !/^\/api\/agents\/i\/[A-Za-z0-9_-]{22}$/.test(path)) {
    throw new Error('服务返回的安装链接无效，请更新 ConfigFlow 后重试')
  }
  if (typeof expiresAt !== 'number' || !Number.isSafeInteger(expiresAt) || expiresAt <= Date.now() / 1000) {
    throw new Error('安装链接已失效，请重新生成')
  }
  const url = new URL(baseUrl)
  url.pathname = `${url.pathname.replace(/\/+$/, '')}${path}`
  return {
    script, url: url.toString(),
    expiry: new Date(expiresAt * 1000).toLocaleString('zh-CN', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false
    })
  }
}

const installCommandError = (error: any, fallback: string) => [404, 405].includes(error.response?.status)
  ? installCommandUpdateMessage
  : error.response?.data?.message || error.message || fallback

const generateCoreInstallCommand = async () => {
  if (coreInstallLoading.value) return
  const request = ++coreInstallRequest
  coreInstallLoading.value = true
  try {
    let baseUrl: URL
    try {
      baseUrl = new URL((localStorage.getItem('serverDomain') || window.location.origin).trim())
    } catch {
      throw new Error('服务地址无效，请先设置完整的 HTTP / HTTPS 地址')
    }
    if (!['http:', 'https:'].includes(baseUrl.protocol) || baseUrl.search || baseUrl.hash || baseUrl.username || baseUrl.password) {
      throw new Error('请将服务地址设置为不含账号、查询参数或片段的 HTTP / HTTPS 地址')
    }
    const response = await agentApi.generateCoreInstallCommand(coreInstallTarget.value)
    if (request !== coreInstallRequest || !shellAio.value || aioCoreInstall.value || !scriptDialogVisible.value) return
    const result = readInstallCommand(response.data, baseUrl)
    if (result.script.split('\n', 2)[1] !== CORE_INSTALL_INTERFACE) {
      throw new Error('安装脚本版本不支持单核心选择，请更新 ConfigFlow 后重试')
    }
    coreInstallScript.value = result.script
    coreInstallScriptUrl.value = result.url
    coreInstallLinkExpiry.value = result.expiry
    notify.success('核心安装命令已生成，请以 root 身份在目标机器执行')
  } catch (error: any) {
    if (request === coreInstallRequest) notify.error(installCommandError(error, '生成核心安装命令失败'))
  } finally {
    if (request === coreInstallRequest) coreInstallLoading.value = false
  }
}

const serviceTypeLabels: Record<Agent['service_type'], string> = {
  mihomo: 'Mihomo',
  surge: 'Surge',
  mosdns: 'MosDNS',
  oxidns: 'OxiDNS'
}

// 服务类型选项
const serviceTypeOptions = [
  { label: 'Mihomo', value: 'mihomo' },
  { label: 'MosDNS', value: 'mosdns' },
  { label: 'OxiDNS', value: 'oxidns' }
]

// 网络模式选项
const networkModeOptions = [
  { label: 'bridge (桥接)', value: 'bridge' },
  { label: 'host (主机网络)', value: 'host' }
]

// 定时刷新
let refreshTimer: number | null = null

// 生成随机端口号（范围：10000-60000）
const generateRandomPort = (): number => {
  return Math.floor(Math.random() * (60000 - 10000 + 1)) + 10000
}

// 统计数据
const onlineCount = computed(() => {
  return agents.value.filter(a => a.status === 'online').length
})

const offlineCount = computed(() => {
  return agents.value.filter(a => a.status === 'offline').length
})

// CPU 使用率图表配置
const chartPalette = useChartPalette()

const cpuChartOption = computed(() => {
  const timestamps = metricsHistory.value.map(m => {
    const date = new Date(m.timestamp)
    return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
  })
  const data = metricsHistory.value.map(m => m.cpu?.usage_percent?.toFixed(1) || 0)

  return {
    title: {
      text: 'CPU 使用率',
      left: 'center',
      top: 10,
      textStyle: { fontSize: 14 }
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const item = params[0]
        return `${item.name}<br/>CPU: ${item.value}%`
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '15%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: timestamps,
      axisLabel: { fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      max: 100,
      axisLabel: {
        formatter: '{value}%',
        fontSize: 11
      }
    },
    series: [{
      name: 'CPU',
      type: 'line',
      smooth: true,
      data: data,
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: withAlpha(chartPalette.value.primary, 0.3) },
            { offset: 1, color: withAlpha(chartPalette.value.primary, 0.05) }
          ]
        }
      },
      lineStyle: { color: chartPalette.value.primary },
      itemStyle: { color: chartPalette.value.primary }
    }]
  }
})

// 内存使用率图表配置
const memoryChartOption = computed(() => {
  const timestamps = metricsHistory.value.map(m => {
    const date = new Date(m.timestamp)
    return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
  })
  const data = metricsHistory.value.map(m => m.memory?.used_percent?.toFixed(1) || 0)

  return {
    title: {
      text: '内存使用率',
      left: 'center',
      top: 10,
      textStyle: { fontSize: 14 }
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const item = params[0]
        return `${item.name}<br/>内存: ${item.value}%`
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '15%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: timestamps,
      axisLabel: { fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      max: 100,
      axisLabel: {
        formatter: '{value}%',
        fontSize: 11
      }
    },
    series: [{
      name: '内存',
      type: 'line',
      smooth: true,
      data: data,
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: withAlpha(chartPalette.value.success, 0.3) },
            { offset: 1, color: withAlpha(chartPalette.value.success, 0.05) }
          ]
        }
      },
      lineStyle: { color: chartPalette.value.success },
      itemStyle: { color: chartPalette.value.success }
    }]
  }
})

// 磁盘使用率图表配置
const diskChartOption = computed(() => {
  const timestamps = metricsHistory.value.map(m => {
    const date = new Date(m.timestamp)
    return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
  })
  const data = metricsHistory.value.map(m => m.disk?.used_percent?.toFixed(1) || 0)

  return {
    title: {
      text: '磁盘使用率',
      left: 'center',
      top: 10,
      textStyle: { fontSize: 14 }
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const item = params[0]
        return `${item.name}<br/>磁盘: ${item.value}%`
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '15%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: timestamps,
      axisLabel: { fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      max: 100,
      axisLabel: {
        formatter: '{value}%',
        fontSize: 11
      }
    },
    series: [{
      name: '磁盘',
      type: 'line',
      smooth: true,
      data: data,
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: withAlpha(chartPalette.value.warning, 0.3) },
            { offset: 1, color: withAlpha(chartPalette.value.warning, 0.05) }
          ]
        }
      },
      lineStyle: { color: chartPalette.value.warning },
      itemStyle: { color: chartPalette.value.warning }
    }]
  }
})

// 网络速度图表配置
const networkChartOption = computed(() => {
  const timestamps = metricsHistory.value.map(m => {
    const date = new Date(m.timestamp)
    return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
  })
  const uploadData = metricsHistory.value.map(m => ((m.network?.speed_sent || 0) / 1024).toFixed(2))
  const downloadData = metricsHistory.value.map(m => ((m.network?.speed_recv || 0) / 1024).toFixed(2))

  return {
    title: {
      text: '网络速度',
      left: 'center',
      top: 10,
      textStyle: { fontSize: 14 }
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        return `${params[0].name}<br/>
          上传: ${params[0].value} KB/s<br/>
          下载: ${params[1].value} KB/s`
      }
    },
    legend: {
      data: ['上传', '下载'],
      bottom: 0,
      textStyle: { fontSize: 11 }
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '15%',
      bottom: '12%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: timestamps,
      axisLabel: { fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: '{value} KB/s',
        fontSize: 11
      }
    },
    series: [
      {
        name: '上传',
        type: 'line',
        smooth: true,
        data: uploadData,
        lineStyle: { color: chartPalette.value.primary },
        itemStyle: { color: chartPalette.value.primary }
      },
      {
        name: '下载',
        type: 'line',
        smooth: true,
        data: downloadData,
        lineStyle: { color: chartPalette.value.success },
        itemStyle: { color: chartPalette.value.success }
      }
    ]
  }
})

// 流量统计图表配置
const trafficChartOption = computed(() => {
  const timestamps = metricsHistory.value.map(m => {
    const date = new Date(m.timestamp)
    return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
  })
  const uploadData = metricsHistory.value.map(m => ((m.network?.bytes_sent || 0) / (1024 * 1024 * 1024)).toFixed(3))
  const downloadData = metricsHistory.value.map(m => ((m.network?.bytes_recv || 0) / (1024 * 1024 * 1024)).toFixed(3))

  return {
    title: {
      text: '流量统计',
      left: 'center',
      top: 10,
      textStyle: { fontSize: 14 }
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const upload = parseFloat(params[0].value).toFixed(2)
        const download = parseFloat(params[1].value).toFixed(2)
        return `${params[0].name}<br/>
          上传: ${upload} GB<br/>
          下载: ${download} GB`
      }
    },
    legend: {
      data: ['上传', '下载'],
      bottom: 0,
      textStyle: { fontSize: 11 }
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '15%',
      bottom: '12%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: timestamps,
      axisLabel: { fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: '{value} GB',
        fontSize: 11
      }
    },
    series: [
      {
        name: '上传',
        type: 'line',
        smooth: true,
        data: uploadData,
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: withAlpha(chartPalette.value.primary, 0.3) },
              { offset: 1, color: withAlpha(chartPalette.value.primary, 0.05) }
            ]
          }
        },
        lineStyle: { color: chartPalette.value.primary },
        itemStyle: { color: chartPalette.value.primary }
      },
      {
        name: '下载',
        type: 'line',
        smooth: true,
        data: downloadData,
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: withAlpha(chartPalette.value.success, 0.3) },
              { offset: 1, color: withAlpha(chartPalette.value.success, 0.05) }
            ]
          }
        },
        lineStyle: { color: chartPalette.value.success },
        itemStyle: { color: chartPalette.value.success }
      }
    ]
  }
})

// 加载 Agent 列表
let agentsLoadGeneration = 0
const trafficNow = ref(Date.now())
const loadAgents = async () => {
  trafficNow.value = Date.now()
  const generation = ++agentsLoadGeneration
  try {
    const { data } = await agentApi.getAll()
    if (generation !== agentsLoadGeneration) return
    trafficNow.value = Date.now()
    agents.value = data
    for (const agent of agents.value) {
      if (agent.latest_upgrade) trackUpgrade(agent.id, agent.latest_upgrade)
      if (agent.latest_deployment && !deploymentBusy(agent.id)) trackDeployment(agent.id, agent.latest_deployment, false)
    }
  } catch (error) {
    if (generation !== agentsLoadGeneration) return
    notify.error('加载 Agent 列表失败')
  }
}

const profileBindingBlocked = (agentId: string) => {
  if (upgradeBusy(agentId)) return true
  const task = deployments[agentId] || agents.value.find(agent => agent.id === agentId)?.latest_deployment
  return !!task && !['succeeded', 'failed', 'rolled_back'].includes(task.status)
}

const bindAgentProfile = async (agent: Agent, profileId: string): Promise<boolean> => {
  if (profileBindingBlocked(agent.id)) {
    notify.warning('请先完成发布或恢复服务，再更改绑定配置空间')
    return false
  }
  const previousProfileId = agent.profile_id || defaultProfileId.value
  if (profileId === previousProfileId) return true
  bindingAgentId.value = agent.id
  try {
    await agentApi.bindProfile(agent.id, profileId)
    agent.profile_id = profileId
    // The previous comparison belongs to the previous binding.
    agent.content_sync = { status: 'checking' }
    void loadAgents()
    notify.success('Agent 绑定的配置空间已更新')
    return true
  } catch (error: any) {
    notify.error(error.response?.data?.message || 'Agent 绑定配置空间失败')
    return false
  } finally {
    bindingAgentId.value = null
  }
}

/* bindAgentProfile 失败时不写回 agent.profile_id，Select 会自动回到原值 */
const handleAgentProfileChange = async (agent: Agent, profileId: string) => {
  if (!profileId || profileId === (agent.profile_id || defaultProfileId.value)) return
  await bindAgentProfile(agent, profileId)
}

// 格式化时间
const formatTime = (timeStr: string) => {
  if (!timeStr) return 'N/A'
  try {
    const date = new Date(timeStr)
    const now = new Date()
    const diff = Math.floor((now.getTime() - date.getTime()) / 1000)

    if (diff < 60) return `${diff} 秒前`
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
    return `${Math.floor(diff / 86400)} 天前`
  } catch (e) {
    return 'N/A'
  }
}

// 判断心跳是否超过5分钟（允许删除记录）
const isHeartbeatExpired = (agent: Agent) => {
  if (!agent.last_heartbeat) return true
  try {
    const date = new Date(agent.last_heartbeat)
    const now = new Date()
    return (now.getTime() - date.getTime()) > 5 * 60 * 1000
  } catch {
    return true
  }
}

// 格式化百分比
const formatPercent = (value: number | undefined) => {
  if (value === undefined || value === null) return 'N/A'
  return `${value.toFixed(1)}%`
}

// 格式化字节数
const formatBytes = (bytes: number | undefined) => {
  if (bytes === undefined || bytes === null) return 'N/A'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = bytes
  let unitIndex = 0
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex++
  }
  return `${size.toFixed(1)} ${units[unitIndex]}`
}

// 格式化网络速度
const formatSpeed = (bytesPerSec: number | undefined) => {
  if (bytesPerSec === undefined || bytesPerSec === null) return '0 B/s'
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let speed = bytesPerSec
  let unitIndex = 0
  while (speed >= 1024 && unitIndex < units.length - 1) {
    speed /= 1024
    unitIndex++
  }
  return `${speed.toFixed(1)} ${units[unitIndex]}`
}

// 格式化网络速度（别名）
const formatNetworkSpeed = formatSpeed

// 获取进度条颜色
const getProgressColor = (percent: number | undefined) => {
  if (percent === undefined || percent === null) return chartPalette.value.primary
  if (percent < 60) return chartPalette.value.success // 绿色
  if (percent < 80) return chartPalette.value.warning // 橙色
  return chartPalette.value.danger // 红色
}

/* ---------- 新 UI 的派生数据 ---------- */

const chipClass = (active: boolean): string =>
  [
    'flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-[13px] transition-colors',
    active
      ? 'border-primary-accent bg-primary-soft text-primary-accent dark:border-primary-accent/40 dark:bg-primary-soft/50 dark:text-foreground'
      : 'border-input bg-card text-muted-foreground hover:border-border-strong dark:border-border/50 dark:bg-background/40'
  ].join(' ')

const INSTALL_TYPES = [
  { value: 'shell', label: 'Shell 安装' },
  { value: 'docker', label: 'Docker 容器' }
]

const SHELL_MODES = [
  { value: 'single', label: '单服务' },
  { value: 'aio', label: 'Mihomo + OxiDNS 同机' }
]

const DOCKER_MODES = [
  { value: 'mihomo', label: 'Mihomo (Clash)' },
  { value: 'mosdns', label: 'MosDNS' },
  { value: 'oxidns', label: 'OxiDNS' },
  { value: 'aio', label: 'All-in-One（三合一）' }
]

const installTypeHint = computed(() =>
  scriptForm.value.installType === 'shell'
    ? '将 Agent 安装为目标机器上的系统服务。'
    : '用 Docker 运行 Agent，无需另行安装 Mihomo / MosDNS / OxiDNS。'
)

const dockerModeHint = computed(
  () =>
    ({
      mihomo: '内置 Mihomo，一个容器运行 Agent + Mihomo。',
      oxidns: '内置 OxiDNS，一个容器运行 Agent + OxiDNS。',
      mosdns: '内置 MosDNS，一个容器运行 Agent + MosDNS。',
      aio: '一个容器运行 Agent + Mihomo + OxiDNS。'
    })[scriptForm.value.dockerMode as string] ?? ''
)

const DOCKER_NOTES: Record<string, { title: string; lines: string[] }> = {
  mihomo: {
    title: 'Docker Mihomo Agent 使用说明',
    lines: [
      '安装后即可使用 Mihomo 代理服务',
      'Agent 与 Mihomo 在同一容器运行，无需另外安装 Mihomo',
      '可自动获取和更新配置，也可在此页面重启服务',
      '代理端口：7890 (HTTP)、7891 (SOCKS5)、9090 (API)'
    ]
  },
  oxidns: {
    title: 'Docker OxiDNS Agent 使用说明',
    lines: [
      'Agent 与 OxiDNS 在同一容器运行',
      '支持完整规则包发布、原生配置检查与失败回滚',
      'DNS 端口：53 (TCP/UDP)；Agent 端口：8082',
      '如果与 MosDNS 同机运行，请使用不同 DNS 监听端口或不同宿主机端口映射'
    ]
  },
  mosdns: {
    title: 'Docker MosDNS Agent 使用说明',
    lines: [
      '安装后即可使用 MosDNS 解析域名',
      'Agent 与 MosDNS 在同一容器运行，无需另外安装 MosDNS',
      '可自动获取和更新配置，也可在此页面重启服务',
      'DNS 端口：53 (TCP/UDP)'
    ]
  },
  aio: {
    title: 'Docker All-in-One Agent 使用说明',
    lines: [
      'Mihomo 负责代理，OxiDNS 负责 DNS 解析',
      'Agent 默认端口：Mihomo 8080，OxiDNS 8082',
      '可通过环境变量 ENABLE_MIHOMO / ENABLE_OXIDNS 控制启用哪些服务',
      '配置保存在 ./aio_data/mihomo 与 ./aio_data/oxidns 目录'
    ]
  }
}

const dockerNote = computed(
  () => DOCKER_NOTES[scriptForm.value.dockerMode as string] ?? DOCKER_NOTES.mihomo
)

const scriptPanelOpen = ref(false)

/** 生成结果按安装方式给出可复制的命令块，避免模板里重复四段几乎相同的结构 */
const commandBlocks = computed(() => {
  if (scriptForm.value.installType === 'shell') {
    return [{
      title: 'Linux（systemd / OpenRC）',
      note: combinedCoreInstall.value ? '以 root 执行；完整下载成功后，先安装核心，再安装 Agent。' : '以 root 执行；完整下载成功后安装 Agent。',
      value: installCommand.value, rows: 3, copy: copyCommand
    }]
  }
  return [
    {
      title: 'Docker Run 命令',
      note: '推荐',
      value: dockerRunCommand.value,
      rows: 8,
      copy: copyDockerRun
    },
    {
      title: 'docker-compose.yml',
      note: '可选，保存后执行 docker compose up -d',
      value: dockerComposeContent.value,
      rows: 15,
      copy: copyDockerCompose
    }
  ]
})

/** 卡片内的资源占用条 */
const metricsOf = (agent: any) => {
  const m = agent.system_metrics || {}
  return [
    {
      label: 'CPU',
      percent: m.cpu?.usage_percent || 0,
      text: formatPercent(m.cpu?.usage_percent),
      color: getProgressColor(m.cpu?.usage_percent),
      detail: ''
    },
    {
      label: '内存',
      percent: m.memory?.used_percent || 0,
      text: formatPercent(m.memory?.used_percent),
      color: getProgressColor(m.memory?.used_percent),
      detail: `${formatBytes(m.memory?.used)} / ${formatBytes(m.memory?.total)}`
    },
    {
      label: '磁盘',
      percent: m.disk?.used_percent || 0,
      text: formatPercent(m.disk?.used_percent),
      color: getProgressColor(m.disk?.used_percent),
      detail: `${formatBytes(m.disk?.used)} / ${formatBytes(m.disk?.total)}`
    }
  ]
}

const metricsSummary = computed(() => {
  const m = currentMetricsAgent.value?.system_metrics
  if (!m) return []
  return [
    {
      label: 'CPU 使用率',
      value: formatPercent(m.cpu?.usage_percent),
      color: getProgressColor(m.cpu?.usage_percent),
      detail: `${m.cpu?.core_count ?? '—'} 核`
    },
    {
      label: '内存使用率',
      value: formatPercent(m.memory?.used_percent),
      color: getProgressColor(m.memory?.used_percent),
      detail: `${formatBytes(m.memory?.used)} / ${formatBytes(m.memory?.total)}`
    },
    {
      label: '磁盘使用率',
      value: formatPercent(m.disk?.used_percent),
      color: getProgressColor(m.disk?.used_percent),
      detail: `${formatBytes(m.disk?.used)} / ${formatBytes(m.disk?.total)}`
    },
    {
      label: '网络速度',
      value: `↑ ${formatNetworkSpeed(m.network?.speed_sent)}`,
      color: 'var(--primary-accent)',
      detail: `↓ ${formatNetworkSpeed(m.network?.speed_recv)}`
    },
    {
      label: '总流量',
      value: `↑ ${formatBytes(m.network?.bytes_sent)}`,
      color: 'var(--success-accent)',
      detail: `↓ ${formatBytes(m.network?.bytes_recv)}`
    }
  ]
})

const metricsCharts = computed(() => [
  { key: 'cpu', option: cpuChartOption.value },
  { key: 'memory', option: memoryChartOption.value },
  { key: 'network', option: networkChartOption.value },
  { key: 'traffic', option: trafficChartOption.value },
  { key: 'disk', option: diskChartOption.value }
])

// 显示监控详情
const showMetricsDetail = async (agent: any) => {
  currentMetricsAgent.value = agent
  metricsDialogVisible.value = true
  metricsLoading.value = true

  try {
    // 获取最近 24 小时的历史数据
    const response = await api.get(`/agents/${agent.id}/metrics/history?hours=24`)
    if (response.data.success) {
      metricsHistory.value = response.data.data.history || []
    } else {
      notify.error('获取监控历史数据失败')
      metricsHistory.value = []
    }
  } catch (error) {
    console.error('Error fetching metrics history:', error)
    notify.error('获取监控历史数据失败')
    metricsHistory.value = []
  } finally {
    metricsLoading.value = false
  }
}

// 安装类型变化时重置相关字段
const onInstallTypeChange = () => {
  // 清空之前生成的命令
  installScriptUrl.value = ''
  installLinkExpiry.value = ''
  dockerComposeContent.value = ''
  dockerRunCommand.value = ''

  if (scriptForm.value.installType === 'docker') {
    // 切换到 Docker 模式时，确保有默认值
    if (!scriptForm.value.containerName) {
      scriptForm.value.containerName = 'configflow-agent'
    }
    if (!scriptForm.value.networkMode) {
      scriptForm.value.networkMode = 'bridge'
    }

    // 触发 docker mode change 以设置正确的默认值
    onDockerModeChange()
  }
}

// Docker 模式变化时
const onDockerModeChange = () => {
  installScriptUrl.value = ''
  installLinkExpiry.value = ''
  dockerComposeContent.value = ''
  dockerRunCommand.value = ''

  if (scriptForm.value.dockerMode === 'mihomo') {
    // mihomo 模式
    scriptForm.value.type = 'mihomo'
  } else if (scriptForm.value.dockerMode === 'oxidns') {
    scriptForm.value.type = 'oxidns'
  } else if (scriptForm.value.dockerMode === 'mosdns') {
    // mosdns 模式
    scriptForm.value.type = 'mosdns'
  } else {
    // aio 模式
    scriptForm.value.type = 'mihomo'
  }
}

// 服务类型变化时更新默认配置路径和重启命令
const onServiceTypeChange = () => {
  Object.assign(scriptForm.value, {
    service_manager: 'auto', service_unit: '', service_binary: '',
    stop_command: '', start_command: '', status_command: ''
  })
  if (scriptForm.value.type === 'mihomo') {
    scriptForm.value.config_path = '/etc/mihomo/config.yaml'
    scriptForm.value.restart_command = ''
    // 如果是 Docker 模式，更新默认服务容器名称
    if (scriptForm.value.installType === 'docker' && !scriptForm.value.serviceContainerName) {
      scriptForm.value.serviceContainerName = 'mihomo'
    }
  } else if (scriptForm.value.type === 'oxidns') {
    scriptForm.value.config_path = '/etc/oxidns/config.yaml'
    scriptForm.value.restart_command = ''
    if (scriptForm.value.installType === 'docker' && !scriptForm.value.serviceContainerName) {
      scriptForm.value.serviceContainerName = 'oxidns'
    }
  } else if (scriptForm.value.type === 'mosdns') {
    scriptForm.value.config_path = '/etc/mosdns/config.yaml'
    scriptForm.value.restart_command = ''
    // 如果是 Docker 模式，更新默认服务容器名称
    if (scriptForm.value.installType === 'docker' && !scriptForm.value.serviceContainerName) {
      scriptForm.value.serviceContainerName = 'mosdns'
    }
  }
}

// 处理生成脚本按钮点击
const handleGenerateScript = () => {
  showGenerateScriptDialog()
}

// 显示生成脚本对话框
const showGenerateScriptDialog = () => {
  installationMode.value = 'ordinary'
  // 清空其他值
  installScript.value = ''
  installScriptUrl.value = ''
  installLinkExpiry.value = ''
  installerDownloader.value = 'curl'
  dockerComposeContent.value = ''
  dockerRunCommand.value = ''
  scriptPanelOpen.value = false

  // Reset every installation field, including both Docker Agent ports.
  scriptForm.value = { ...createScriptForm(), port: generateRandomPort() }
  lifecyclePanelOpen.value = false
  installHelpOpen.value = false
  coreOnlyPanelOpen.value = false
  dockerOptionsOpen.value = false

  // 打开对话框 - v-if 会确保表单完全重新渲染
  scriptDialogVisible.value = true
}

// 重置表单
const resetForm = () => {
  scriptRequest += 1
  scriptLoading.value = false
  installScript.value = ''
  installScriptUrl.value = ''
  installLinkExpiry.value = ''
  dockerComposeContent.value = ''
  dockerRunCommand.value = ''
  scriptPanelOpen.value = false
}

watch(scriptForm, resetForm, { deep: true, flush: 'sync' })
watch(scriptDialogVisible, resetForm, { flush: 'sync' })
watch([installScriptUrl, dockerComposeContent, dockerRunCommand], async (commands) => {
  if (!commands.some(Boolean)) return
  await nextTick()
  installCommandSection.value?.scrollIntoView?.({ block: 'start', behavior: 'auto' })
})

// 生成安装脚本
const generateScript = async () => {
  if (scriptLoading.value) return
  if (!scriptForm.value.name.trim()) {
    notify.warning('请输入 Agent 名称')
    return
  }

  // Shell 安装时检查配置文件路径是否为文件而非目录
  if (scriptForm.value.installType === 'shell') {
    if (!/^[A-Za-z0-9_-]+$/.test(scriptForm.value.name.trim())) {
      notify.warning('Agent 名称只能包含字母、数字、短横线或下划线')
      return
    }
    const form = shellServiceSettings.value
    const configPaths = shellAio.value ? [form.mihomoConfigPath, form.oxidnsConfigPath] : [form.config_path]
    if (configPaths.some(path => path.trim() && !path.trim().match(/\.\w+$/))) {
      lifecyclePanelOpen.value = true
      notify.warning('配置文件路径应指向一个文件（如 config.yaml），而不是文件夹')
      return
    }
    if (shellAio.value) {
      if (![form.mihomoAgentPort, form.oxidnsAgentPort].every(port => Number.isInteger(port) && port >= 1024 && port <= 65535)) {
        notify.warning('Agent 端口应为 1024 到 65535 之间的整数')
        return
      }
      if (form.mihomoAgentPort === form.oxidnsAgentPort) {
        notify.warning('两个 Agent 必须使用不同的端口')
        return
      }
      if (aioCoreInstall.value && [form.mihomoAgentPort, form.oxidnsAgentPort].includes(1053)) {
        notify.warning('Mihomo DNS 使用 1053，请为 Agent 选择其他端口')
        return
      }
    } else {
      if (singleCoreInstall.value && (!Number.isInteger(form.port) || form.port < 1024 || form.port > 65535)) {
        notify.warning('Agent 端口应为 1024 到 65535 之间的整数')
        return
      }
      if (singleCoreInstall.value && form.type === 'mihomo' && form.port === 1053) {
        notify.warning('Mihomo DNS 使用 1053，请为 Agent 选择其他端口')
        return
      }
      const unit = (form.service_unit.trim() || form.type).replace(/\.service$/, '')
      const restart = form.restart_command.trim()
      const standardRestart = !restart || [
        `systemctl restart ${unit}`, `systemctl restart ${unit}.service`, `rc-service ${unit} restart`
      ].includes(restart)
      const customLifecycle = form.service_manager === 'command' || (form.service_manager === 'auto' && !standardRestart)
      if (customLifecycle && ![form.stop_command, form.start_command, form.status_command].every(value => value.trim())) {
        lifecyclePanelOpen.value = true
        notify.warning('自定义服务需要填写停止、启动和状态检查命令，才能在发布失败时回滚')
        return
      }
    }
  }

  const request = ++scriptRequest
  const includeCore = combinedCoreInstall.value
  scriptLoading.value = true
  const loadingToast = notify.loading(scriptForm.value.installType === 'docker' ? '正在生成 Docker 部署命令...' : '正在生成安装命令...')

  try {
    if (scriptForm.value.installType === 'docker') {
      // 生成 Docker Compose 和 Docker Run
      const serverUrl = localStorage.getItem('serverDomain') || window.location.origin

      const dockerParams = {
        server_url: serverUrl,
        agent_name: scriptForm.value.containerName || scriptForm.value.name,
        agent_ip: scriptForm.value.agent_ip || '',
        network_mode: scriptForm.value.networkMode,
        enable_mihomo: scriptForm.value.dockerMode === 'mihomo' || scriptForm.value.dockerMode === 'aio',
        enable_oxidns: scriptForm.value.dockerMode === 'oxidns' || scriptForm.value.dockerMode === 'aio',
        oxidns_port: scriptForm.value.oxidnsAgentPort,
        enable_mosdns: scriptForm.value.dockerMode === 'mosdns',
        mihomo_port: scriptForm.value.mihomoAgentPort,
        mosdns_port: scriptForm.value.mosdnsAgentPort,
        // 根据模式设置数据目录
        data_dir: scriptForm.value.dockerMode === 'aio' ? './aio_data' :
                  (scriptForm.value.dockerMode === 'oxidns' ? './oxidns_data' : scriptForm.value.dockerMode === 'mosdns' ? './mosdns_data' : './mihomo_data')
      }

      // 使用统一的 Docker API
      const [composeResponse, runResponse] = await Promise.all([
        api.get('/agents/docker-agent-compose', { params: dockerParams }),
        api.get('/agents/docker-agent-run', { params: dockerParams })
      ])

      if (request !== scriptRequest || !scriptDialogVisible.value) return
      dockerComposeContent.value = composeResponse.data
      dockerRunCommand.value = runResponse.data
      notify.success('Docker 部署命令已生成，请复制到目标机器执行')
    } else {
      let serverUrl = localStorage.getItem('serverDomain') || window.location.origin
      let parsed: URL
      try { parsed = new URL(serverUrl.trim()) } catch { throw new Error('服务地址无效，请先设置完整的 HTTP / HTTPS 地址') }
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.search || parsed.hash || parsed.username || parsed.password) {
        throw new Error('请将服务地址设置为不含账号、查询参数或片段的 HTTP / HTTPS 地址')
      }
      serverUrl = parsed.toString().replace(/\/+$/, '')
      const form = { ...shellServiceSettings.value }
      const lifecycleParams: Record<string, string> = {}
      if (form.service_manager !== 'auto') lifecycleParams.service_manager = form.service_manager
      for (const key of ['service_unit', 'service_binary', 'stop_command', 'start_command', 'status_command'] as const) {
        const value = form[key].trim()
        if (value) lifecycleParams[key] = value
      }
      const shellParams: AgentInstallScriptParams = shellAio.value ? {
        name: form.name.trim(),
        type: 'aio',
        agent_ip: form.agent_ip.trim(),
        server_url: serverUrl,
        mihomo_agent_port: form.mihomoAgentPort,
        oxidns_agent_port: form.oxidnsAgentPort,
        mihomo_config_path: form.mihomoConfigPath.trim() || '/etc/mihomo/config.yaml',
        oxidns_config_path: form.oxidnsConfigPath.trim() || '/etc/oxidns/config.yaml',
        mihomo_service_unit: form.mihomoServiceUnit.trim() || 'mihomo',
        oxidns_service_unit: form.oxidnsServiceUnit.trim() || 'oxidns',
        mihomo_service_binary: form.mihomoServiceBinary.trim() || 'mihomo',
        oxidns_service_binary: form.oxidnsServiceBinary.trim() || 'oxidns',
        ...(includeCore ? { install_core: true } : {})
      } : {
        name: form.name.trim(),
        type: form.type,
        port: form.port,
        agent_ip: form.agent_ip.trim(),
        config_path: form.config_path.trim(),
        restart_command: form.restart_command.trim(),
        server_url: serverUrl,
        ...lifecycleParams,
        ...(includeCore ? { install_core: true } : {})
      }
      const response = await agentApi.generateInstallCommand(shellParams)
      if (request !== scriptRequest || !scriptDialogVisible.value) return
      const result = readInstallCommand(response.data, parsed)
      if (includeCore && result.script.split('\n', 2)[1] !== AGENT_CORE_INSTALL_INTERFACE) {
        throw new Error('安装脚本版本不支持同时安装核心，请更新 ConfigFlow 后重试')
      }

      // The preview and download are the same immutable server-generated snapshot.
      installScript.value = result.script
      installScriptUrl.value = result.url
      installLinkExpiry.value = result.expiry

      notify.success('安装命令已生成，请复制到目标机器执行')
    }
  } catch (error: any) {
    if (request === scriptRequest) {
      const errorMsg = scriptForm.value.installType === 'shell'
        ? installCommandError(error, '生成脚本失败')
        : error.response?.data?.message || error.message || '生成脚本失败'
      notify.error(errorMsg)
    }
  } finally {
    if (request === scriptRequest) scriptLoading.value = false
    notify.dismiss(loadingToast)
  }
}

const copyAgentContent = async (text: string, message: string) => {
  if (!text) return
  try {
    await copyText(text)
    notify.success(message)
  } catch {
    notify.error('复制失败，请手动复制')
  }
}

const copyCommand = () => copyAgentContent(installCommand.value, '命令已复制到剪贴板')
const copyDockerCompose = () => copyAgentContent(dockerComposeContent.value, 'Docker Compose 已复制到剪贴板')
const copyDockerRun = () => copyAgentContent(dockerRunCommand.value, 'Docker Run 命令已复制到剪贴板')

// 推送配置
const submissionFailures = ref<Record<string, Partial<AgentDeployment>>>({})
const pushingIds = ref<Set<string>>(new Set())
const pushingAll = ref(false)

type PushOutcome = 'submitted' | 'done' | 'failed' | 'busy'

/**
 * 推送：数据包沿拓扑通道送出；mihomo / mosdns / oxidns 走事务发布，结果由 useAgentDeployments 跟踪并提示，
 * 这里只处理「提交」本身的成败。
 */
const pushOne = async (agent: Agent, quiet = false): Promise<PushOutcome> => {
  if (deploymentBusy(agent.id)) return 'busy'
  delete submissionFailures.value[agent.id]
  const deploymentId = createDeploymentId()
  const transactional = agent.service_type !== 'surge'
  if (transactional && !beginSubmission(agent.id, deploymentId)) return 'busy'
  pushingIds.value = new Set([...pushingIds.value, agent.id])
  topology.value?.burst(`agent:${agent.id}`, 16)
  try {
    const { data } = await agentApi.pushConfig(agent.id, deploymentId)
    if (data.deployment_id === deploymentId && typeof data.status === 'string') {
      endSubmission(agent.id, deploymentId, data)
      return 'submitted'
    }
    if (transactional) {
      endSubmission(agent.id, deploymentId, 'unknown')
      return 'submitted'
    }
    if (data?.success === false || data?.restart?.success === false) {
      submissionFailures.value[agent.id] = { ...data, error: data.restart?.success === false ? `配置已写入，但重启失败：${data.restart.message || '服务未确认启动。'}` : data.error || data.message, error_details: data.restart?.error_details || data.error_details }
      if (!quiet) notify.error(`「${agent.name}」配置发布失败`, deploymentErrorSummary(submissionFailures.value[agent.id]))
      return 'failed'
    }
    return 'done'
  } catch (error: any) {
    if (error.response?.data?.deployment_id) {
      if (error.response.data.deployment_id === deploymentId) {
        endSubmission(agent.id, deploymentId, error.response.data)
        return 'submitted'
      }
      endSubmission(agent.id, deploymentId, 'rejected')
      trackDeployment(agent.id, error.response.data)
      return 'submitted'
    }
    if (transactional && (!error.response || error.response.status >= 500)) {
      endSubmission(agent.id, deploymentId, 'unknown')
      return 'submitted'
    }
    if (transactional && endSubmission(agent.id, deploymentId, 'rejected')) return 'submitted'
    submissionFailures.value[agent.id] = { ...error.response?.data, error: error.response?.data?.error || error.response?.data?.message || '发布请求失败，请重试。' }
    if (!quiet) notify.error(`「${agent.name}」配置发布失败`, deploymentErrorSummary(submissionFailures.value[agent.id]))
    return 'failed'
  } finally {
    const next = new Set(pushingIds.value)
    next.delete(agent.id)
    pushingIds.value = next
  }
}

const pushConfig = async (agent: Agent) => {
  if (deploymentBusy(agent.id)) return
  if ((await pushOne(agent)) === 'done') {
    notify.success(`已推送至「${agent.name}」`)
    void loadAgents()
  }
}

const pushAll = async () => {
  const online = agents.value.filter(a => a.status === 'online' && !deploymentBusy(a.id))
  if (!online.length || pushingAll.value) return
  pushingAll.value = true
  const results = await submitAgentRequests(online, agent => pushOne(agent, true))
  pushingAll.value = false
  const sent = results.filter(r => r === 'submitted' || r === 'done').length
  const failed = results.filter(r => r === 'failed').length
  const skipped = agents.value.length - online.length
  const tail = `${failed ? ` · ${failed} 台失败` : ''}${skipped ? ` · ${skipped} 台离线或发布中跳过` : ''}`
  if (failed) notify.warning(`已向 ${sent} 台 Agent 提交推送${tail}`)
  else notify.success(`已向 ${sent} 台 Agent 提交推送${tail}`)
  void loadAgents()
}

// 重启 Agent
const restartAgent = async (agent: Agent) => {
  if (deployments[agent.id]?.status === 'ready') {
    await activateDeployment(agent.id)
    return
  }
  const ok = await confirm('确定要重启此 Agent 的服务吗？服务将会短暂中断。', {
    title: '重启服务',
    confirmText: '重启'
  })
  if (!ok) return

  try {
    const loadingToast = notify.loading('正在重启服务...')

    try {
      await agentApi.restart(agent.id)
      notify.success('服务重启成功')
    } finally {
      notify.dismiss(loadingToast)
    }
  } catch (error: any) {
    notify.error(error.response?.data?.message || '服务重启失败')
  }
}

const updateAgent = async (agent: Agent) => {
  if (agent.deployment_method === 'docker') {
    dockerUpdateAgent.value = agent
    return
  }
  if (upgradeBusy(agent.id) || deploymentBusy(agent.id)) return
  const ok = await confirm('更新将保留身份和核心配置，自动完成迁移并重启 Agent。网页会持续查询最终结果。', {
    title: '更新 Agent', confirmText: '立即更新'
  })
  if (!ok) return
  try {
    await startUpgrade(agent.id)
  } catch (error: any) {
    notify.error(error.response?.data?.message || '更新请求未确认，请查询更新结果')
    void loadAgents()
  }
}

// 卸载 Agent
const uninstallAgent = async (agent: Agent) => {
  const ok = await confirmDanger(
    `确定要卸载远程服务器上的 Agent「${agent.name}」吗？\n\n此操作将：\n• 停止 Agent 服务\n• 删除 Agent 程序文件\n• 删除服务配置（systemd/OpenRC）\n• 从管理列表中移除\n\n此操作不可恢复。`,
    { title: '卸载 Agent', confirmText: '确定卸载' }
  )
  if (!ok) return

  try {
    const loadingToast = notify.loading('正在卸载 Agent...')

    try {
      await agentApi.uninstall(agent.id)
      notify.success('Agent 卸载命令已发送，远程服务器正在执行卸载...')
      // 等待几秒后刷新列表
      setTimeout(() => {
        loadAgents()
      }, 3000)
    } finally {
      notify.dismiss(loadingToast)
    }
  } catch (error: any) {
    notify.error(error.response?.data?.message || '卸载失败')
  }
}

// 删除 Agent 记录
const deleteAgent = async (agent: Agent) => {
  const ok = await confirmDanger(
    `确定删除 Agent「${agent.name}」的管理记录吗？记录删除后无法恢复，但不会卸载远程机器上的 Agent 程序。如需移除程序，请使用「卸载 Agent」。`,
    { title: '删除记录', confirmText: '确定删除' }
  )
  if (!ok) return

  try {
    await agentApi.delete(agent.id)
    notify.success('记录删除成功')
    loadAgents()
  } catch (error: any) {
    notify.error(error.response?.data?.message || '删除失败')
  }
}

// 根据 Agent 类型动态生成日志选项
const logPathOptions = computed(() => {
  const common = [
    { label: '主 Agent 日志', value: '/var/log/configflow-agent.log' },
    { label: 'Supervisor 主日志', value: '/var/log/supervisor/supervisord.log' },
  ]
  const mihomo = [
    { label: 'Mihomo 输出日志', value: '/var/log/supervisor/mihomo.log' },
    { label: 'Mihomo 错误日志', value: '/var/log/supervisor/mihomo.err.log' },
  ]
  const mosdns = [
    { label: 'MosDNS 错误日志', value: '/etc/mosdns/mosdns.err.log' },
  ]
  const oxidns = [
    { label: 'OxiDNS 输出日志', value: '/var/log/supervisor/oxidns.log' },
    { label: 'OxiDNS 错误日志', value: '/var/log/supervisor/oxidns.err.log' }
  ]
  const type = currentAgent.value?.service_type
  if (type === 'oxidns') return [...common, ...oxidns]
  if (type === 'mihomo') return [...common, ...mihomo]
  if (type === 'mosdns') return [...common, ...mosdns]
  return [...common, ...mihomo, ...mosdns]
})

// 判断是否为主 Agent 日志
const isMainAgentLog = computed(() => {
  return selectedLogPath.value === '/var/log/configflow-agent.log' && customLogPath.value === ''
})

// 查看日志
const viewLogs = async (agent: Agent) => {
  currentAgent.value = agent
  selectedLogPath.value = '/var/log/configflow-agent.log'
  customLogPath.value = ''
  logsDialogVisible.value = true
  await loadLoggingConfig()
  await loadLogs()
}

// 加载日志
const loadLogs = async () => {
  if (!currentAgent.value) return

  try {
    // 确定要读取的日志路径
    let logPath = ''
    if (selectedLogPath.value === 'custom') {
      if (!customLogPath.value) {
        logs.value = '请输入自定义日志路径'
        return
      }
      logPath = customLogPath.value
    } else {
      logPath = selectedLogPath.value
    }

    // 调用 API 获取日志
    const { data } = await agentApi.getLogs(currentAgent.value.id, 200, logPath)
    if (data.success) {
      logs.value = data.logs || '暂无日志'
    } else {
      logs.value = `获取日志失败: ${data.message}`
    }

    // 等待DOM更新后滚动到底部
    await nextTick()
    scrollToBottom()
  } catch (error: any) {
    logs.value = `获取日志失败: ${error.response?.data?.message || error.message || '未知错误'}`
    notify.error('获取日志失败')
  }
}

// 滚动到日志底部
const scrollToBottom = () => {
  const pane = logsPaneRef.value
  if (pane) pane.scrollTop = pane.scrollHeight
}

// 刷新日志
const refreshLogs = async () => {
  const loadingToast = notify.loading('正在刷新日志...')

  try {
    await loadLogs()
    notify.success('日志刷新成功')
  } finally {
    notify.dismiss(loadingToast)
  }
}

// 清空日志
const clearLogs = async () => {
  if (!currentAgent.value) return

  let logPath = selectedLogPath.value === 'custom' ? customLogPath.value : selectedLogPath.value
  if (!logPath) {
    notify.warning('请先选择日志文件')
    return
  }

  const ok = await confirmDanger('确定要清空该日志文件吗？此操作不可恢复。', {
    title: '清空日志',
    confirmText: '确定清空'
  })
  if (!ok) return

  const loadingToast = notify.loading('正在清空日志...')

  try {
    const { data } = await agentApi.clearLog(currentAgent.value.id, logPath)
    if (data.success) {
      notify.success('日志已清空')
      await loadLogs()
    } else {
      notify.error(data.message || '清空日志失败')
    }
  } catch (error: any) {
    notify.error(error.response?.data?.message || '清空日志失败')
  } finally {
    notify.dismiss(loadingToast)
  }
}

// 日志路径切换
const onLogPathChange = async (newPath: string) => {
  // 确保 selectedLogPath 已更新
  selectedLogPath.value = newPath
  if (newPath !== 'custom') {
    customLogPath.value = ''
    await loadLogs()
  }
}

// 验证并加载自定义路径
const validateAndLoadCustomPath = async () => {
  if (!currentAgent.value || !customLogPath.value) {
    notify.warning('请输入日志路径')
    return
  }

  validatingPath.value = true
  try {
    const { data } = await agentApi.validateLogPath(currentAgent.value.id, customLogPath.value)

    if (data.success && data.valid) {
      notify.success('路径验证成功')
      await loadLogs()
    } else {
      notify.error(data.error || '路径验证失败')
      logs.value = `路径验证失败: ${data.error || '未知错误'}`
    }
  } catch (error: any) {
    notify.error('路径验证失败')
    logs.value = `路径验证失败: ${error.response?.data?.message || error.message}`
  } finally {
    validatingPath.value = false
  }
}

// 加载日志配置状态
const loadLoggingConfig = async () => {
  if (!currentAgent.value) return

  try {
    const { data } = await agentApi.getLoggingConfig(currentAgent.value.id)
    if (data.success) {
      loggingEnabled.value = data.enabled !== false // 默认为 true
    }
  } catch (error: any) {
    console.error('获取日志配置失败:', error)
  }
}

// 切换日志开关
const toggleLogging = async (enabled: boolean) => {
  if (!currentAgent.value) return

  togglingLogging.value = true
  try {
    const { data } = await agentApi.setLoggingConfig(currentAgent.value.id, enabled)

    if (data.success) {
      notify.success(enabled ? '日志已启用' : '日志已禁用')
    } else {
      notify.error('修改日志开关失败')
      loggingEnabled.value = !enabled // 回滚状态
    }
  } catch (error: any) {
    notify.error('设置日志开关失败')
    loggingEnabled.value = !enabled // 回滚状态
  } finally {
    togglingLogging.value = false
  }
}

// 启动定时刷新
const startAutoRefresh = () => {
  // 每 10 秒刷新一次
  refreshTimer = window.setInterval(() => {
    loadAgents()
  }, 10000)
}

// 停止定时刷新
const stopAutoRefresh = () => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

/* ---------- 拓扑与配置版本 ---------- */
const topology = ref<InstanceType<typeof FlowMap> | null>(null)
const isNarrow = useMediaQuery('(max-width: 639px)')
const profileStore = useProfileStore()

const revisionOf = (profileId?: string) => {
  const revision = profiles.value.find(p => p.id === (profileId || defaultProfileId.value))?.revision
  return typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= 0 ? revision : undefined
}
const activeRevision = computed(() => revisionOf(profileStore.activeProfileId.value))
const activeProfileName = computed(
  () => profileStore.activeProfile.value?.name || profileStore.activeProfileId.value
)

const syncOf = (agent: Agent) => getAgentConfigSync(agent, revisionOf(agent.profile_id), deployments[agent.id])

const topoColumns = computed<FlowColumn[]>(() => [
  { key: 'hub', title: '配置中心', nodes: [{ id: 'hub', title: 'ConfigFlow', kind: 'hub', fit: true }] },
  {
    key: 'agents',
    title: 'Agent',
    nodes: agents.value.map(a => ({
      id: `agent:${a.id}`,
      title: a.name,
      meta: `${a.host}:${a.port}`,
      icon: Server,
      status: (a.status === 'online' ? 'online' : 'offline') as 'online' | 'offline'
    }))
  }
])

const topoEdges = computed<FlowEdge[]>(() =>
  agents.value.map(a => ({
    from: 'hub',
    to: `agent:${a.id}`,
    weight: 0.7,
    tone: (['mosdns', 'oxidns'].includes(a.service_type) ? 'info' : 'success') as FlowTone,
    dead: a.status !== 'online'
  }))
)

/* 推送与跨页动作 */
const router = useRouter()
const runPending = () => {
  if (consumeAction(router, 'push-all')) pushAll()
}
watch(() => router?.currentRoute.value.query.run, run => run === 'push-all' && runPending())

onMounted(async () => {
  await Promise.all([loadAgents(), refreshProfiles().catch(() => undefined)])
  startAutoRefresh()
  runPending()
})

onUnmounted(() => {
  resetCoreInstallCommand()
  settingsPageActive = false
  stopAutoRefresh()
})
</script>

<style scoped>
@media (width < 640px) {
  .agents-page-header :deep(.cf-page-actions) {
    width: 100%;
    margin-left: 0;
    gap: var(--cf-sp-1);
  }

  .agents-page-header :deep(.cf-page-actions > button:first-child) {
    flex: 0 0 var(--cf-touch);
  }
}

.agent-pulse {
  animation: agent-ping 2s infinite;
}

@keyframes agent-ping {
  0% {
    box-shadow: 0 0 0 0 oklch(from var(--success-accent) l c h / 60%);
  }
  100% {
    box-shadow: 0 0 0 8px transparent;
  }
}
</style>
