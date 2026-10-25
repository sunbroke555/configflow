"""The OxiDNS editor's whole-object save is guarded against rules-page writes."""
from backend.test_rule_compose_route import HEADERS, make_native, oxidns, set_dns, space  # noqa: F401


def settings_url():
    return '/api/oxidns/settings'


def test_settings_get_returns_etag(space):
    client, repository = space
    response = client.get(settings_url(), headers=HEADERS)
    etag = response.headers['ETag']
    assert etag.startswith('"') and len(etag) == 66
    assert client.get(settings_url(), headers=HEADERS).headers['ETag'] == etag
    assert set_dns(client, {'inline': 'blackhole'}).status_code == 200
    assert client.get(settings_url(), headers=HEADERS).headers['ETag'] != etag


def test_settings_post_stale_if_match_409_with_code(space):
    client, repository = space
    loaded = client.get(settings_url(), headers=HEADERS)
    # The rules page changes the same object in between.
    assert set_dns(client, {'inline': 'nxdomain'}).status_code == 200
    before = repository.export_all()
    response = client.post(settings_url(), headers={**HEADERS, 'If-Match': loaded.headers['ETag']},
                           json={**loaded.get_json(), 'prefer_ipv4': False})
    assert response.status_code == 409
    assert response.get_json() == {'success': False, 'code': 'oxidns_changed_elsewhere',
                                   'message': 'OxiDNS 设置已在其他页面修改（例如策略规则的 DNS），请重新加载后再保存'}
    assert repository.export_all() == before


def test_settings_post_accepts_validator_weakened_by_gzip_proxy(space):
    """nginx gzip turns the ETag into W/"..."; saving with it must not look like a conflict."""
    client, repository = space
    loaded = client.get(settings_url(), headers=HEADERS)
    weak = 'W/' + loaded.headers['ETag']
    saved = client.post(settings_url(), headers={**HEADERS, 'If-Match': weak},
                        json={**loaded.get_json(), 'prefer_ipv4': False})
    assert saved.status_code == 200
    assert oxidns(repository)['prefer_ipv4'] is False
    # A weakened stale validator is still a conflict.
    assert set_dns(client, {'inline': 'nxdomain'}).status_code == 200
    stale = client.post(settings_url(), headers={**HEADERS, 'If-Match': 'W/' + saved.headers['ETag']},
                        json={**saved.get_json()['settings'], 'prefer_ipv4': True})
    assert stale.status_code == 409 and stale.get_json()['code'] == 'oxidns_changed_elsewhere'


def test_settings_post_returns_new_etag(space):
    client, repository = space
    loaded = client.get(settings_url(), headers=HEADERS)
    first = client.post(settings_url(), headers={**HEADERS, 'If-Match': loaded.headers['ETag']},
                        json={**loaded.get_json(), 'prefer_ipv4': False})
    assert first.status_code == 200
    assert first.headers['ETag'] == client.get(settings_url(), headers=HEADERS).headers['ETag']
    # "Save and publish" saves again with the new validator.
    second = client.post(settings_url(), headers={**HEADERS, 'If-Match': first.headers['ETag']},
                         json=first.get_json()['settings'])
    assert second.status_code == 200


def test_settings_post_without_if_match_unchanged(space):
    client, repository = space
    loaded = client.get(settings_url(), headers=HEADERS).get_json()
    assert set_dns(client, {'inline': 'nxdomain'}).status_code == 200
    response = client.post(settings_url(), headers=HEADERS, json={**loaded, 'prefer_ipv4': False})
    assert response.status_code == 200 and response.get_json()['success'] is True
    assert oxidns(repository)['prefer_ipv4'] is False


def test_settings_save_drops_follow_for_changed_routes(space):
    client, repository = space
    assert set_dns(client, {'inline': 'follow', 'set': 'follow', 'off': 'follow'}).status_code == 200
    loaded = client.get(settings_url(), headers=HEADERS)
    settings = loaded.get_json()
    assert sorted(settings['rule_dns_follow']) == ['inline', 'off', 'set']
    # Moving a follow rule by hand in the editor makes it an explicit choice.
    settings.update(direct_rules=[], proxy_rules=['inline'])
    response = client.post(settings_url(), headers={**HEADERS, 'If-Match': loaded.headers['ETag']}, json=settings)
    assert response.status_code == 200, response.get_json()
    assert sorted(oxidns(repository)['rule_dns_follow']) == ['off', 'set']
    assert client.get('/api/rules/dns', headers=HEADERS).get_json()['rules']['inline'] == {
        'state': 'explicit', 'route': 'proxy'}
    # Native drafts read the route from the YAML.
    assert set_dns(client, {'inline': 'follow'}).status_code == 200
    make_native(client)
    settings = client.get(settings_url(), headers=HEADERS).get_json()
    applied = client.post('/api/oxidns/common-settings/apply', headers=HEADERS, json={
        'settings': settings, 'changes': {'direct_rules': [], 'direct_rulesets': ['set']}}).get_json()['settings']
    assert sorted(applied['rule_dns_follow']) == ['off', 'set']
    settings['native_config'] = applied['native_config']
    assert client.post(settings_url(), headers=HEADERS, json=settings).status_code == 200
    assert sorted(oxidns(repository)['rule_dns_follow']) == ['off', 'set']


def test_settings_if_match_checked_inside_the_save(space, monkeypatch):
    """A rules-page write landing after the request started still fails If-Match."""
    import contextvars
    from flask import Flask
    import backend.routes.oxidns as route
    from backend.routes import register_blueprints
    client, repository = space
    make_native(client)
    loaded = client.get(settings_url(), headers=HEADERS)
    settings = {**loaded.get_json(), 'native_config': loaded.get_json()['native_config'] + '\n# editor\n'}
    normalize = route.normalize_oxidns_settings

    def concurrent(value, **options):
        # Another request thread has its own config context.
        other = Flask('other')
        register_blueprints(other)
        response = contextvars.Context().run(lambda: other.test_client().put(
            '/api/rules/dns', headers=HEADERS, json={'routes': {'proxied': 'blackhole'}}))
        assert response.status_code == 200
        monkeypatch.setattr(route, 'normalize_oxidns_settings', normalize)
        return normalize(value, **options)

    monkeypatch.setattr(route, 'normalize_oxidns_settings', concurrent)
    response = client.post(settings_url(), headers={**HEADERS, 'If-Match': loaded.headers['ETag']}, json=settings)
    assert response.status_code == 409 and response.get_json()['code'] == 'oxidns_changed_elsewhere'
    stored = oxidns(repository)
    assert stored['rule_actions'] == {'proxied': {'action': 'blackhole'}}
    assert 'cf_dns_blackhole' in stored['native_config'] and '# editor' not in stored['native_config']
    # The validator returned is that of the committed object, not a later re-read.
    current = client.get(settings_url(), headers=HEADERS)
    saved = client.post(settings_url(), headers={**HEADERS, 'If-Match': current.headers['ETag']},
                        json=current.get_json())
    assert saved.status_code == 200 and saved.headers['ETag'] == route._etag(saved.get_json()['settings'])
