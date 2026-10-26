"""Managed regex exclusions remain source-local across live Mihomo providers."""
from copy import deepcopy

import pytest
import yaml

from backend.converters.mihomo import generate_mihomo_config
from backend.test_dynamic_proxy_chains import dynamic_profile, expire_source_download, provider_text
from backend.utils.dialer_references import filter_provider_candidates, provider_proxy_name
from backend.utils.group_candidates import CandidateValidationError, filter_candidates, validate_filters
from backend.utils.provider_delivery import prepare_provider_bundle


@pytest.fixture(autouse=True)
def isolated_source_cache(tmp_path, monkeypatch):
    from backend.common import config
    from backend.common.config_repository import ProfileRepository
    monkeypatch.setattr(config, '_repository', ProfileRepository(tmp_path))
    config.reset_config_context()
    yield
    config.reset_config_context()


def candidates(main, group_name, payloads):
    group = next(group for group in main['proxy-groups'] if group['name'] == group_name)
    selected = []
    for name in group['use']:
        provider = main['proxy-providers'][name]
        raw = [{'name': name, 'type': 'http'} for name in payloads[provider['url']]]
        raw = filter_provider_candidates(raw, provider.get('filter', ''), provider.get('exclude-filter', ''))
        selected.extend({'name': provider_proxy_name(item, provider.get('override', {})),
                         'original': item['name']} for item in raw)
    return [item['original'] for item in filter_provider_candidates(selected, group.get('filter', ''))]


def config_with_sources(**fields):
    return {
        'profile_id': 'default',
        'nodes': [{'id': 'manual', 'name': 'Manual-expired', 'type': 'http',
                   'server': 'manual.test', 'port': 80}],
        'subscriptions': [{'id': 'sub', 'name': 'Feed', 'url': 'https://feed.test'}],
        'subscription_aggregations': [{'id': 'agg', 'name': 'Combined', 'subscriptions': []}],
        'proxy_groups': [{'id': 'selected', 'name': 'Selected', 'type': 'select',
                          'subscriptions': ['sub'], 'aggregations': ['agg'],
                          'manual_nodes': ['manual'], **fields}],
        'rule_configs': [],
    }


def test_candidate_filter_preserves_records_order_and_empty_include():
    nodes = [{'name': 'US-new', 'params': {'password': 'unchanged'}}, {'name': 'US-expired'}, {'name': 'HK-new'}]
    assert filter_candidates(nodes, exclude='expired') == [nodes[0], nodes[2]]
    actual = filter_candidates(nodes, include='^US', exclude='expired')
    assert actual == [nodes[0]] and actual[0] is nodes[0]
    with pytest.raises(CandidateValidationError):
        filter_candidates(nodes, exclude='[')


def test_exclusion_validation_and_rendering_trim_inline_flags_consistently():
    expression = '  (?i)expired  '
    validate_filters({'exclude_regex': expression, 'aggregation_exclude_regex': expression})
    config = config_with_sources(exclude_regex=expression)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    group = main['proxy-groups'][0]
    provider = main['proxy-providers'][group['use'][0]]
    assert provider['exclude-filter'] == '(?i)expired'
    nodes = [{'name': 'US-EXPIRED'}, {'name': 'US-new'}]
    assert filter_candidates(nodes, exclude=expression) == filter_provider_candidates(
        nodes, exclude=provider['exclude-filter']) == [nodes[1]]
    assert config['proxy_groups'][0]['exclude_regex'] == expression


def test_mihomo_source_filters_are_independent_and_followed_without_filtering_manual_nodes():
    config = config_with_sources(regex='^US', exclude_regex='expired',
                                 aggregation_regex='^HK', aggregation_exclude_regex='slow')
    config['proxy_groups'] += [
        {'id': 'follow', 'name': 'Follow', 'type': 'select', 'follow_group': 'selected'},
        {'id': 'all', 'name': 'All', 'type': 'select', 'subscriptions': ['sub']},
    ]
    before = deepcopy(config)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    groups = {group['name']: group for group in main['proxy-groups']}
    assert groups['Selected']['proxies'] == groups['Follow']['proxies'] == ['Manual-expired']
    assert groups['Selected']['use'] == groups['Follow']['use']
    assert groups['All']['use'] == ['Feed']
    assert 'exclude-filter' not in main['proxy-providers']['Feed']
    assert len({provider['path'] for provider in main['proxy-providers'].values()}) == len(main['proxy-providers'])
    for name in groups['Selected']['use']:
        provider = main['proxy-providers'][name]
        original = next(value for original, value in main['proxy-providers'].items()
                        if original in ('Feed', 'Combined') and value['url'] == provider['url'])
        assert provider['health-check'] == original['health-check']
        assert provider['interval'] == original['interval']
    payloads = {provider['url']: ['US-new', 'US-expired', 'HK-new', 'HK-slow']
                for provider in main['proxy-providers'].values()}
    assert candidates(main, 'Selected', payloads) == ['US-new', 'HK-new']
    assert candidates(main, 'Follow', payloads) == ['US-new', 'HK-new']
    assert candidates(main, 'All', payloads) == ['US-new', 'US-expired', 'HK-new', 'HK-slow']
    assert config == before


@pytest.mark.parametrize('exclude', ['expired|US`old', r'expired|US\`old'])
def test_exclusion_only_keeps_other_sources_and_literal_backticks(exclude):
    config = config_with_sources(exclude_regex=exclude)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    payloads = {provider['url']: (['US-new', 'US-expired', 'US`old'] if '/subscriptions/' in provider['url']
                                 else ['HK-new', 'HK-expired'])
                for provider in main['proxy-providers'].values()}
    assert candidates(main, 'Selected', payloads) == ['US-new', 'HK-new', 'HK-expired']


@pytest.mark.parametrize('include', ['^US-', '^US-`^HK-'])
@pytest.mark.parametrize('exclude', ['expired', '^US-expired(?:-new)?$', '(?i)^us-expired(?:-new)?$'])
def test_chain_inherits_exclusions_on_refresh_and_keeps_manual_candidates(include, exclude, monkeypatch):
    config = dynamic_profile()
    config['proxy_groups'][1].update(regex=include, exclude_regex=exclude, manual_nodes=['b'])
    config['nodes'][1]['name'] = 'US-expired-manual'
    membership = ['US-first', 'US-expired', 'HK-first']
    calls = []

    def fetch(*args):
        calls.append(args)
        return provider_text(membership), 'rendered_yaml'

    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {group['name']: group for group in main['proxy-groups']}
    assert groups['Exit']['proxies'] == ['US-expired-manual']
    assert len(groups['Chain']['proxies']) == 1
    provider = main['proxy-providers'][groups['Chain']['use'][0]]
    if '`' in include:
        # Buckets keep the per-source copy and its exclude-filter.
        assert provider['exclude-filter'] == exclude
    else:
        # One shared rule is folded into the filter: no source copy, no exclude-filter.
        assert 'exclude-filter' not in provider and provider['filter']
        assert not any('_筛选' in name for name in main['proxy-providers'])
    assert provider['override']['dialer-proxy'] == 'A'
    assert len(calls) == 1
    membership[:] = ['HK-second', 'US-expired-new', 'US-second']
    cached_bundle = prepare_provider_bundle(config, main)
    cached_payloads = {item['url']: [node['name'] for node in yaml.safe_load(item['content'])['proxies']]
                       for item in cached_bundle}
    cached_expected = ['US-first', 'HK-first'] if '`' in include else ['US-first']
    assert candidates(main, 'Exit', cached_payloads) == candidates(main, 'Chain', cached_payloads) == cached_expected
    assert len(calls) == 1  # Provider polling must respect the upstream source interval.
    expire_source_download()
    bundle = prepare_provider_bundle(config, main)
    payloads = {item['url']: [node['name'] for node in yaml.safe_load(item['content'])['proxies']]
                for item in bundle}
    expected = ['US-second', 'HK-second'] if '`' in include else ['US-second']
    assert candidates(main, 'Exit', payloads) == candidates(main, 'Chain', payloads) == expected
    assert len(calls) == 2


@pytest.mark.parametrize('native', ['slow', ''])
def test_chain_combines_managed_and_native_exclusions(native):
    config = dynamic_profile()
    config['proxy_groups'][1].update(exclude_regex='expired', **{'exclude-filter': native})
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    group = next(group for group in main['proxy-groups'] if group['name'] == 'Chain')
    provider = main['proxy-providers'][group['use'][0]]
    selected = filter_provider_candidates(
        [{'name': name} for name in ['US-new', 'US-expired', 'US-slow']],
        provider.get('filter', ''), provider.get('exclude-filter', ''))
    expected = ['US-new'] if native else ['US-new', 'US-slow']
    assert selected == [{'name': name} for name in expected]


@pytest.mark.parametrize('include, exclude', [('', 'expired'), ('^US', '(?i)EXPIRED'), ('', 'US`old'), ('US', r'(?<=US-)\w+ed$')])
def test_one_shared_rule_becomes_the_group_filter_without_a_source_copy(include, exclude):
    config = config_with_sources(regex=include, exclude_regex=exclude, aggregations=[])
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    group = main['proxy-groups'][0]
    assert group['use'] == ['Feed'] and list(main['proxy-providers']) == ['Feed']
    assert 'exclude-filter' not in main['proxy-providers']['Feed'] and '`' not in group['filter']
    # The group filter never touches the group's own members.
    assert group['proxies'] == ['Manual-expired']
    nodes = [{'name': name} for name in ['US-new', 'US-EXPIRED', 'US-expired', 'HK-new', 'US`old']]
    assert filter_provider_candidates(nodes, group['filter']) == filter_candidates(nodes, include, exclude)


@pytest.mark.parametrize('fields', [{'regex': '^US`^HK', 'exclude_regex': 'expired', 'aggregations': []},
                                    {'exclude_regex': 'expired'}])
def test_bucketed_include_or_differing_source_rules_keep_the_source_copy(fields):
    # Buckets reorder the group's own members too; differing rules need one filter per source.
    config = config_with_sources(**fields)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    group = main['proxy-groups'][0]
    copy = next(name for name in group['use'] if name.startswith('Selected_Feed_筛选'))
    assert main['proxy-providers'][copy]['exclude-filter'] == 'expired'
