"""Explicit, profile-scoped device groups: review before save and migration."""
from copy import deepcopy

from flask import jsonify, request

from backend.common.auth import require_auth
from backend.common.config import get_repository, reset_config_context
from backend.common.config_repository import ProfileInUse, ProfileValidationError
from backend.common.device_groups import DeviceGroupsError, compile_groups, lookup_ip, source_networks
from backend.common import device_group_workspace as workspace
from backend.routes import profiles_bp


def _body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ProfileValidationError('请求内容必须是对象')
    if not isinstance(value.get('base_url', ''), str):
        raise ProfileValidationError('base_url 必须是文本')
    return value


def _read(profile_id):
    return get_repository().get_publish_snapshot(profile_id)


def _check_revision(config, revision, value):
    if value.get('revision') != workspace.revision(config, revision):
        raise ProfileInUse('配置、基础设施或 Agent 绑定已改变，请重新加载并预览')


def _response(value):
    response = jsonify(value)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _snapshot(profile_id):
    config, revision = _read(profile_id)
    result = workspace.snapshot(config, revision)
    from backend.agents.device_group_publish import latest
    result['active_publication'] = latest(get_repository(), profile_id)
    return result


def _observations(config, result):
    """Sample outside every save transaction; absent samples prove nothing."""
    import ipaddress
    from backend.agents.device_group_activity import activity
    from backend.common.agent_manager import get_agent_manager
    observed = activity(config, get_agent_manager())
    result['infrastructure_samples'] = observed['infrastructure_samples']
    removed = [ipaddress.ip_network(value) for value in result['routing_changes']['removed']]
    result['routing_changes']['affected_clients'] = [row['ip'] for row in observed['clients']
        if any((address := ipaddress.ip_address(row['ip'])).version == network.version and address in network
               for network in removed)]
    return result


@profiles_bp.route('/<profile_id>/device-groups', methods=['GET', 'PUT'])
@require_auth
def device_groups(profile_id):
    if request.method == 'GET':
        return _response(_snapshot(profile_id))
    value = _body()
    config, revision = _read(profile_id)
    _check_revision(config, revision, value)
    proposed = workspace.candidate(config, value)
    checked = workspace.preview(config, revision, proposed, value.get('base_url', ''))
    if checked['blockers']:
        raise ProfileValidationError('；'.join(checked['blockers']))
    if value.get('preview_token') != checked['preview_token']:
        raise ProfileInUse('保存内容与预览不一致，请重新预览后保存')

    def save(current):
        current_revision = get_repository().config_revision(profile_id)
        _check_revision(current, current_revision, value)
        candidate = workspace.candidate(current, value)
        if workspace.preview_token(current, current_revision, candidate, value.get('base_url', '')) != value.get('preview_token'):
            raise ProfileInUse('预览已过期，请重新检查后保存')
        # The final repository gate owns native metadata and patching. Keep the
        # prior YAML here so its persisted baseline remains the source of trust.
        current.update({field: deepcopy(candidate[field]) for field in workspace.DRAFT_FIELDS})

    get_repository().update_profile_transaction(profile_id, save)
    reset_config_context()
    return _response(_snapshot(profile_id))


@profiles_bp.route('/<profile_id>/device-groups/preview', methods=['POST'])
@require_auth
def preview_device_groups(profile_id):
    value = _body()
    config, revision = _read(profile_id)
    _check_revision(config, revision, value)
    proposed = workspace.candidate(config, value)
    return _response(_observations(config, workspace.preview(config, revision, proposed, value.get('base_url', ''))))


def _migration(config):
    from backend.common.device_group_migration import plan_migration
    return plan_migration(config)


def _migration_preview(config, revision, base_url):
    from backend.common.oxidns_device_groups import normalize_transition
    plan = _migration(config)
    proposed = deepcopy(plan['config'])
    blockers = list(plan.get('blockers', []))
    if not blockers:
        try:
            normalize_transition(config, proposed)
        except ValueError as error:
            blockers.append(str(error))
    if blockers:
        # Invalid legacy members/native ownership must be reviewable without
        # compiling an invalid candidate or changing the stored document.
        result = {'success': True, 'profile_id': config['profile_id'],
            'revision': workspace.revision(config, revision), 'config_revision': revision,
            'preview_token': '', 'draft': workspace.draft(proposed),
            'generated_changes': [], 'blockers': blockers, 'warnings': [],
            'affected_agents': workspace.targets(config), 'copied_blocking_rules': [],
            'excluded_after_match_rules': [],
            'routing_changes': {'added': [], 'removed': [], 'affected_clients': []}}
    else:
        result = workspace.preview(config, revision, proposed, base_url)
    result['warnings'].extend({'code': 'migration', 'message': message} if isinstance(message, str) else message
                             for message in plan.get('warnings', []))
    result['migration'] = {key: item for key, item in plan.items() if key != 'config'}
    return result


@profiles_bp.route('/<profile_id>/device-groups/migration/preview', methods=['POST'])
@require_auth
def preview_device_group_migration(profile_id):
    value = _body()
    config, revision = _read(profile_id)
    _check_revision(config, revision, value)
    return _response(_observations(config, _migration_preview(config, revision, value.get('base_url', ''))))


@profiles_bp.route('/<profile_id>/device-groups/migration', methods=['POST'])
@require_auth
def migrate_device_groups(profile_id):
    from backend.common.oxidns_device_groups import normalize_transition
    value = _body()
    config, revision = _read(profile_id)
    _check_revision(config, revision, value)
    if config.get('device_groups') is not None:
        raise ProfileInUse('这个配置空间已经升级为设备组，请刷新页面')
    checked = _migration_preview(config, revision, value.get('base_url', ''))
    if checked['blockers']:
        raise ProfileValidationError('；'.join(checked['blockers']))
    if value.get('preview_token') != checked['preview_token']:
        raise ProfileInUse('升级预览已过期，请重新预览')

    def migrate(current):
        current_revision = get_repository().config_revision(profile_id)
        _check_revision(current, current_revision, value)
        current_plan = _migration(current)
        candidate = deepcopy(current_plan['config'])
        normalize_transition(current, candidate)
        if current_plan.get('blockers') or workspace.preview_token(current, current_revision, candidate, value.get('base_url', '')) != value.get('preview_token'):
            raise ProfileInUse('升级内容已改变，请重新预览')
        unpatched = current_plan['config']
        current.update({field: deepcopy(unpatched[field]) for field in (*workspace.DRAFT_FIELDS, 'oxidns')})

    get_repository().update_profile_transaction(profile_id, migrate, allow_device_group_migration=True)
    reset_config_context()
    return _response(_snapshot(profile_id))


@profiles_bp.route('/<profile_id>/device-groups/lookup', methods=['GET'])
@require_auth
def lookup_device_group(profile_id):
    import ipaddress
    config, _ = _read(profile_id)
    compiled = compile_groups(config)
    result = lookup_ip(config, request.args.get('ip'), compiled=compiled)
    address = ipaddress.ip_address(result['ip'])
    included = address.version == 4 and any(address in ipaddress.ip_network(value)
        for value in source_networks(config, '0.0.0.0', compiled=compiled))
    return _response({'success': True, 'ip': result['ip'], 'group_id': result['group_id'],
        'group_name': result['group']['name'], 'mode': result['mode'], 'matched_member': result['matched_member'],
        'overridden_members': [{'member': row['member'], 'group_name': compiled['by_id'][row['group_id']]['name']}
                               for row in result['matches'][1:]],
        'infrastructure_exclusion': '基础设施地址按全局规则处理' if result['infrastructure_excluded'] else None,
        'policy_routing_included': included})


@profiles_bp.route('/<profile_id>/device-groups/activity', methods=['GET'])
@require_auth
def device_group_activity(profile_id):
    from backend.agents.device_group_activity import activity
    from backend.common.agent_manager import get_agent_manager
    config, _ = _read(profile_id)
    return _response(activity(config, get_agent_manager()))


@profiles_bp.route('/<profile_id>/device-groups/diagnostics', methods=['GET'])
@require_auth
def device_group_diagnostics(profile_id):
    from backend.common.device_group_diagnostics import diagnostics
    from backend.common.agent_manager import get_agent_manager
    config, _ = _read(profile_id)
    return _response(diagnostics(config, get_agent_manager(), check=True,
                                 config_loader=lambda: _read(profile_id)[0]))


@profiles_bp.route('/<profile_id>/device-groups/publish', methods=['POST'])
@require_auth
def publish_device_groups(profile_id):
    from backend.agents.device_group_publish import create
    from backend.common.agent_manager import get_agent_manager
    value = create(get_repository(), get_agent_manager(), profile_id, _body())
    return _response({'success': True, 'publication': value}), 202


@profiles_bp.route('/<profile_id>/device-groups/publish/<batch_id>', methods=['GET'])
@require_auth
def device_group_publication(profile_id, batch_id):
    from backend.agents.device_group_publish import get
    return _response({'success': True, 'publication': get(get_repository(), profile_id, batch_id)})


@profiles_bp.route('/<profile_id>/device-groups/publish/<batch_id>/retry', methods=['POST'])
@require_auth
def retry_device_group_publication(profile_id, batch_id):
    from backend.agents.device_group_publish import retry
    from backend.common.agent_manager import get_agent_manager
    value = retry(get_repository(), get_agent_manager(), profile_id, batch_id, _body())
    return _response({'success': True, 'publication': value}), 202
