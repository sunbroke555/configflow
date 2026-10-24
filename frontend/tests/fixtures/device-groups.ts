import type { DeviceGroupActivity, DeviceGroupPublication, DeviceGroupsPreview, DeviceGroupsSnapshot } from '@/types/deviceGroups'

export const deviceGroupsState = (): DeviceGroupsSnapshot => ({
  success: true, profile_id: 'office', profile_name: '办公室', revision: 'r1', config_revision: 10, migrated: true,
  migration: { available: false }, capabilities: { oxidns_mode: 'template', keep_blocking_editable: true },
  device_groups: { version: 1, groups: [
    { id: 'dg_direct', name: '直连设备', members: ['192.0.2.0/25'], mode: 'direct', keep_blocking: false, dns: 'follow' },
    { id: 'dg_filter', name: '加强过滤', members: ['192.0.2.60'], mode: 'proxy', policy: '节点选择', keep_blocking: true },
    { id: 'default', name: '默认组', members: ['192.0.2.10'] },
  ] },
  policy_routing_sources: { include: ['192.0.2.0/24'], exclude_groups: ['dg_direct'] },
  device_notes: { '192.0.2.60': '客厅设备' }, policy_groups: [{ id: 'p1', name: '节点选择' }],
  effective_groups: [{ id: 'dg_direct', ownership_networks: ['192.0.2.0/25'], effective_networks: ['192.0.2.0/25'], blocking_count: 0 }, { id: 'dg_filter', ownership_networks: ['192.0.2.60/32'], effective_networks: ['192.0.2.60/32'], blocking_count: 4 }],
  infrastructure: { addresses: [{ ip: '192.0.2.1', kind: 'router' }], unresolved: [] },
  affected_agents: [
    { id: 'm1', name: 'Mihomo 1', host: '192.0.2.2', service_type: 'mihomo', status: 'online', target_fingerprint: 'mfp', version_state: 'supported', core_version: '1.19.32' },
    { id: 'o1', name: 'OxiDNS 1', host: '192.0.2.3', service_type: 'oxidns', status: 'online', target_fingerprint: 'ofp' },
  ], warnings: [],
})
export const deviceGroupsReview = (state = deviceGroupsState()): DeviceGroupsPreview => ({
  success: true, profile_id: state.profile_id, revision: state.revision, config_revision: state.config_revision, preview_token: `preview-${state.revision}`,
  draft: { device_groups: state.device_groups, policy_routing_sources: state.policy_routing_sources, device_notes: state.device_notes },
  generated_changes: [{ service_type: 'oxidns', before: 'plugins: []', after: 'plugins:\n  - tag: cf_dg_dispatch' }, { service_type: 'mihomo', before: 'rules: []', after: 'rules:\n  - SUB-RULE,(RULE-SET,cf_dg_set_test,src),cf_dg_seg_test' }],
  copied_blocking_rules: [{ id: 'ads', name: '广告规则', dns_action: '黑洞', policy: 'REJECT' }], excluded_after_match_rules: [{ id: 'after', name: 'MATCH 后拦截' }],
  routing_changes: { added: [], removed: ['192.0.2.60/32'], affected_clients: ['192.0.2.60'] }, affected_agents: state.affected_agents, blockers: [], warnings: [],
})
export const deviceGroupsActivity = (): DeviceGroupActivity => ({ success: true, partial: true, sample_size: 100, sample_limited: false,
  clients: [{ ip: '192.0.2.60', count: 62, agent_ids: ['o1'] }, { ip: '192.0.2.1', count: 38, agent_ids: ['o1'] }],
  agents: [{ id: 'o1', name: 'OxiDNS 1', state: 'available', sample_size: 100 }, { id: 'o2', name: 'OxiDNS 2', state: 'offline' }], infrastructure_samples: [{ ip: '192.0.2.1', kind: 'router', count: 38, share: .38 }] })
export const deviceGroupsPublication = (): DeviceGroupPublication => ({ batch_id: 'batch-one', profile_id: 'office', config_revision: 10, revision: 'r1', state: 'paused', phase: 'mihomo', message: 'Mihomo 待激活，OxiDNS 尚未发布。',
  targets: [{ agent_id: 'm1', name: 'Mihomo 1', service_type: 'mihomo', state: 'paused', deployment_id: 'dep-one', deployment: { deployment_id: 'dep-one', status: 'ready', message: '配置已通过内核校验' } }, { agent_id: 'o1', name: 'OxiDNS 1', service_type: 'oxidns', state: 'queued' }],
  actions: { can_retry: false, can_reconcile: true, activate_agent_ids: ['m1'], requires_new_preview: false },
})
