"""Abandon an unfinished first-use setup.

The wizard always works in its own new profile. Abandoning removes that
profile (and the subscription the wizard added) only while nothing outside
ConfigFlow depends on it: no bound Agent, no started installation, no router
record whose device-side changes would lose their manager, and not the
default profile. Otherwise only the wizard ends and the profile is kept.
"""
from backend.common.config_repository import ProfileInUse, ProfileNotFound, ProfileValidationError
from backend.common import setup_flow as flow

DISMISSED_KEY = 'setup_suggestion_dismissed'


def _owned_subscription(repository, session):
    subscription_id = 'setup_sub_' + session['id']
    if subscription_id not in session.get('subscription_ids', []):
        return None
    return next((item for item in repository.get_shared()['subscriptions'] if item['id'] == subscription_id), None)


def plan(repository, session):
    if session.get('completed'):
        raise flow.SetupError('向导已完成，无需放弃', 409)
    profile_id = session['profile_id']
    profile = repository.get_profile(profile_id)
    kept, guidance = None, ''
    agents = repository.get_profile_agent_bindings(profile_id)['agents']
    if agents:
        names = '、'.join(agent.get('name') or agent.get('id', '') for agent in agents[:3])
        kept = f'已有设备在使用它（{names}{" 等" if len(agents) > 3 else ""}）'
        guidance = '如不再需要，先在 Agent 页解绑或删除这些设备，再到「配置空间」删除。'
    elif (profile.get('routers') or {}).get('items'):
        kept = '已添加主路由，路由器上的改动需要在这里管理或撤销'
        guidance = '如不再需要，先在「上游路由」撤销改动并删除路由器，再到「配置空间」删除。'
    elif _has_installation(repository, profile_id):
        kept = '设备安装已经开始，安装命令仍可能被执行'
        guidance = '如不再需要，可在 Agent 页确认没有新设备后，再到「配置空间」删除。'
    elif repository.default_profile_id() == profile_id:
        kept = '它是当前的默认配置空间'
        guidance = '如不再需要，先在「配置空间」把其他配置设为默认，再删除它。'
    subscription = None if kept else _owned_subscription(repository, session)
    return {'session_id': session['id'], 'name': session['name'],
            'profile': {'id': profile_id, 'name': profile.get('name') or session['name']},
            'delete_profile': kept is None, 'kept_reason': kept, 'guidance': guidance,
            'subscription': {'id': subscription['id'], 'name': subscription.get('name', '')} if subscription else None}


def _has_installation(repository, profile_id):
    from backend.agents.gateway_installation import has_installation
    return has_installation(repository, profile_id)


def _dismiss_suggestion(repository):
    def update(system):
        system.setdefault('system_config', {})[DISMISSED_KEY] = True
    repository.update_system_transaction(update)


def abandon(repository, session):
    """Return the applied plan; the caller holds the session lock."""
    result = plan(repository, session)
    _dismiss_suggestion(repository)
    if result['delete_profile']:
        try:
            repository.delete_profile(session['profile_id'])
        except ProfileNotFound:
            pass
        except (ProfileInUse, ProfileValidationError) as error:
            # A device or router appeared after the plan was read; keep it.
            result.update(delete_profile=False, kept_reason=str(error), subscription=None,
                          guidance='如不再需要，请处理上述占用后再到「配置空间」删除。')
    if result['delete_profile']:
        if result['subscription']:
            subscription_id = result['subscription']['id']
            try:
                repository.update_shared_transaction(lambda shared: {
                    **shared, 'subscriptions': [item for item in shared['subscriptions'] if item['id'] != subscription_id]})
                from backend.utils import subscription_health
                subscription_health.forget(subscription_id)
            except (ProfileInUse, ProfileValidationError):
                result['subscription_kept'] = True  # Another profile now uses it.
        flow.remove(repository, session['id'])
    else:
        session['abandoned'] = True
        session['abandoned_at'] = flow._now()
        flow.write(repository, session)
    return result
