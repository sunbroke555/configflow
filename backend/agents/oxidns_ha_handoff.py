"""Durable VIP handoff gates for the existing two-member publication journal."""
import time

TIMEOUT = 30


class HandoffPending(Exception):
    def __init__(self, message, *, unknown=False):
        super().__init__(message)
        self.unknown = unknown


def _save(manager, job):
    from .oxidns_ha_publish import _save as save
    return save(manager, job)


def _request(manager, agent, method, operation_id):
    try:
        result = manager.oxidns_ha_handoff(agent, method, operation_id)
    except Exception as error:
        raise HandoffPending('虚拟 IP 迁移操作结果待确认，尚未继续更新原主机', unknown=True) from error
    if not isinstance(result, dict) or result.get('error') or result.get('success') is False:
        raise HandoffPending('无法确认虚拟 IP 迁移状态，尚未继续更新原主机', unknown=True)
    return result


def prepare(manager, job, members, bundle):
    """Refuse unsupported peers before either core can be changed."""
    if 'handoff_required' in job:
        return
    # A backend upgrade must not strand a previously accepted publication on
    # old Agents, which cannot themselves be upgraded while that job is active.
    if job.get('handoff_protocol') != 1:
        job['handoff_required'] = False
        _save(manager, job)
        return
    # Rule-only transactions hot-reload their data; neither core is restarted.
    required = not bundle.manifest.get('reload_only')
    paired = all((member.get('oxidns_ha') or {}).get('enabled') is True for member in members.values())
    if required:
        for member in members.values():
            capability = manager._deployment_request(member, 'get', 'capabilities', timeout=5)
            protocols = capability.get('oxidns_ha_handoff_protocols') or []
            if not isinstance(protocols, list) or 1 not in protocols:
                raise ValueError(f'{member.get("name") or member["id"]} 的 Agent 不支持发布前迁移虚拟 IP，请先升级两台 Agent')
            if paired:
                record = _request(manager, member, 'get', job['id']).get('handoff')
                if record and record.get('operation_id') != job['id']:
                    raise ValueError('设备仍有其他虚拟 IP 迁移记录，请先确认上一任务的清理结果')
    job['handoff_required'] = bool(required and paired)
    _save(manager, job)


def _views(manager, members):
    views = {}
    for ident, member in members.items():
        try:
            result = manager.oxidns_ha_status(member)
        except Exception as error:
            raise HandoffPending('无法确认两台设备的虚拟 IP 状态，尚未继续更新原主机', unknown=True) from error
        managed, runtime = result.get('managed') or {}, result.get('runtime')
        if (result.get('error') or not isinstance(runtime, dict)
                or managed.get('active') is not True or managed.get('pending')):
            raise HandoffPending('设备上的高可用状态尚未确认，尚未继续更新原主机', unknown=True)
        views[ident] = runtime
    return views


def _healthy_holder(views, holder):
    return ([ident for ident, runtime in views.items() if runtime.get('vip_present') is True] == [holder]
            and all(runtime.get('keepalived_running') is True and runtime.get('healthy') is True
                    and runtime.get('vip_healthy') is True for runtime in views.values()))


def ensure(manager, job, members):
    """Called immediately before the original holder's upload or activation."""
    if not job.get('handoff_required'):
        return
    source, target = job['order'][1], job['order'][0]
    if not job.get('handoff'):
        job['handoff'] = {'operation_id': job['id'], 'source_agent_id': source,
                          'target_agent_id': target, 'requested_at': time.time(),
                          'confirmed': False, 'released': False, 'confirmations': 0}
    handoff = job['handoff']
    if handoff.get('released'):
        raise ValueError('虚拟 IP 迁移已结束，未继续激活旧候选配置')
    job.update(phase='handoff', status='checking', active_agent_id=target,
               message='正在将虚拟 IP 迁移到已更新的设备，并确认 DNS 解析')
    _save(manager, job)  # Persist intent before a possibly lost POST response.
    if not handoff.get('confirmed') and time.time() - handoff['requested_at'] >= TIMEOUT:
        raise ValueError('虚拟 IP 迁移或解析确认超时，原主机未更新')
    result = _request(manager, members[source], 'get', handoff['operation_id'])
    record = result.get('handoff')
    if not record:
        result = _request(manager, members[source], 'post', handoff['operation_id'])
        record = result.get('handoff')
    if not record or record.get('operation_id') != handoff['operation_id'] or record.get('active') is not True:
        raise HandoffPending('虚拟 IP 让位记录尚未确认，原主机未更新', unknown=True)
    views = _views(manager, members)
    if not _healthy_holder(views, target):
        handoff.update(confirmed=False, confirmations=0)
        _save(manager, job)
        raise HandoffPending('等待已更新的设备独占虚拟 IP，并从两台确认虚拟 IP 的 DNS 解析')
    handoff['confirmations'] = handoff.get('confirmations', 0) + 1
    handoff['confirmed'] = handoff['confirmations'] >= 2
    _save(manager, job)
    if not handoff['confirmed']:
        raise HandoffPending('虚拟 IP 已迁移，正在再次确认 DNS 解析后更新原主机')


def release(manager, job):
    """Do not release the pair reservation until the owned preference is gone."""
    handoff = job.get('handoff')
    if not handoff or handoff.get('released'):
        return
    source = manager.get_agent_by_id(handoff['source_agent_id'])
    if not source:
        raise HandoffPending('原主机记录不可用，无法确认虚拟 IP 让位清理', unknown=True)
    job.update(phase='handoff', status='checking', message='正在恢复正常的虚拟 IP 选举')
    _save(manager, job)
    record = _request(manager, source, 'get', handoff['operation_id']).get('handoff')
    if record:
        if record.get('operation_id') != handoff['operation_id']:
            raise HandoffPending('设备上的虚拟 IP 迁移记录已改变，需确认清理结果', unknown=True)
        _request(manager, source, 'delete', handoff['operation_id'])
        record = _request(manager, source, 'get', handoff['operation_id']).get('handoff')
        if record:
            raise HandoffPending('虚拟 IP 让位记录尚未清理，正在等待设备确认', unknown=True)
    handoff.update(released=True, released_at=time.time())
    _save(manager, job)


def finish(manager, job, members):
    if not job.get('handoff'):
        return
    release(manager, job)
    preferred = next((ident for ident, member in members.items()
                      if (member.get('oxidns_ha') or {}).get('role') == 'primary'), None)
    if not preferred or not _healthy_holder(_views(manager, members), preferred):
        if time.time() - job['handoff']['released_at'] >= TIMEOUT:
            raise ValueError('两台已更新，但恢复正常选举后的虚拟 IP 解析检查未通过')
        raise HandoffPending('两台已更新，正在等待正常选举和虚拟 IP 解析恢复')


def settle_failure(manager, job):
    """Avoid an immediate retry racing preemption after a failed handoff."""
    handoff = job.get('handoff') or {}
    if not handoff.get('released') or time.time() - handoff['released_at'] >= TIMEOUT:
        return
    members = {ident: manager.get_agent_by_id(ident) for ident in job['children']}
    if not all(members.values()):
        return
    views = _views(manager, members)
    # An unhealthy core still needs the original failure exposed for repair;
    # when both cores are healthy, wait for their normal election to settle.
    if all(view.get('healthy') is True and view.get('keepalived_running') is True for view in views.values()):
        preferred = next((ident for ident, member in members.items()
                          if (member.get('oxidns_ha') or {}).get('role') == 'primary'), None)
        if preferred and not _healthy_holder(views, preferred):
            raise HandoffPending('迁移已撤销，正在等待正常选举恢复后结束本次发布')
