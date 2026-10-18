"""Connection failures explain the next step without following credentialed redirects."""
import json
from unittest.mock import Mock

import pytest
import requests

from backend.routers.base import RouterError
from backend.routers.ikuai import IKuaiDriver
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def response(status=200, location=None, body=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body or {'code': 0}).encode()
    if location is not None:
        result.headers['Location'] = location
    return result


@pytest.fixture
def driver(monkeypatch):
    instance = IKuaiDriver('http://192.168.2.1', 'admin', 'secret-password')
    monkeypatch.setattr(instance.session, 'post', Mock())
    yield instance
    instance.close()


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_https_redirect_shows_management_address_without_retry(driver, status):
    driver.session.post.return_value = response(status, 'https://192.168.2.1/Action/login')
    with pytest.raises(RouterError) as error:
        driver.probe()
    assert f'跳转到 https://192.168.2.1（HTTP {status}）' in str(error.value)
    assert '编辑路由器' in str(error.value)
    assert '/Action/login' not in str(error.value)
    driver.session.post.assert_called_once()
    assert driver.session.post.call_args.kwargs['allow_redirects'] is False
    assert driver.session.verify is True


@pytest.mark.parametrize(('location', 'target'), [
    ('https://192.168.2.1:8443/login?token=secret-token', 'https://192.168.2.1:8443'),
    ('https://user:secret-pass@192.168.2.1:443/secret-path#secret-fragment', 'https://192.168.2.1'),
    ('https://[fd00::1]:8443/login', 'https://[fd00::1]:8443'),
    ('//router.local:8080/login', 'http://router.local:8080'),
])
def test_redirect_preserves_host_port_but_omits_credentials_and_paths(driver, location, target):
    driver.session.post.return_value = response(302, location)
    with pytest.raises(RouterError) as error:
        driver.probe()
    assert f'跳转到 {target}（HTTP 302）' in str(error.value)
    assert 'secret-' not in str(error.value)
    assert '/login' not in str(error.value)


@pytest.mark.parametrize('location', [
    None, '', '/login?token=secret-token', '../login',
    'http://192.168.2.1:80/login', 'https://[invalid',
    'https://router.local:bad-port/login', 'javascript:secret-token',
])
def test_same_address_or_unknown_redirect_does_not_claim_https_is_required(driver, location):
    driver.session.post.return_value = response(302, location)
    with pytest.raises(RouterError) as error:
        driver.probe()
    assert 'HTTP 302' in str(error.value)
    assert '核对最终地址' in str(error.value)
    assert '跳转到 https://' not in str(error.value)
    assert 'secret-' not in str(error.value)


def test_redirect_after_login_keeps_explanation_and_stops(driver):
    driver.session.post.side_effect = [response(), response(302, '/login')]
    with pytest.raises(RouterError, match='转到了其他页面'):
        driver.probe()
    assert driver.session.post.call_count == 2


@pytest.mark.parametrize(('status', 'hint'), [
    (401, '用户名、密码'), (403, '管理员权限'), (404, '未找到管理接口'),
    (405, '反向代理'), (408, '超时'), (429, '请求过于频繁'),
    (500, '管理服务发生错误'), (502, '后端服务'), (503, '暂时不可用'),
    (504, '后端响应超时'), (400, '管理地址'), (418, '管理地址'),
])
def test_http_failure_has_reason_and_action(driver, status, hint):
    driver.session.post.return_value = response(status, body={'message': 'secret-server-content'})
    with pytest.raises(RouterError) as error:
        driver.probe()
    assert hint in str(error.value)
    assert f'HTTP {status}' in str(error.value)
    assert 'secret-' not in str(error.value)


@pytest.mark.parametrize(('exception', 'hint'), [
    (requests.exceptions.SSLError, '证书'),
    (requests.ConnectTimeout, '连接 iKuai 超时'),
    (requests.ReadTimeout, '等待 iKuai 响应超时'),
    (requests.Timeout, '等待 iKuai 响应超时'),
    (requests.ConnectionError, '管理服务已开启'),
    (requests.exceptions.ChunkedEncodingError, '通信失败'),
    (requests.RequestException, '通信失败'),
])
def test_transport_failure_has_helpful_message_without_raw_exception(driver, exception, hint):
    driver.session.post.side_effect = exception('secret-network-details')
    with pytest.raises(RouterError) as error:
        driver.probe()
    assert hint in str(error.value)
    assert 'secret-' not in str(error.value)
    driver.session.post.assert_called_once()


def test_probe_api_persists_explanation_for_router_list(authenticated_app, monkeypatch):  # noqa: F811
    client, _, _, admin, _ = authenticated_app
    created = client.post('/api/routers', headers=admin, json={
        'name': 'iKuai', 'type': 'ikuai', 'endpoint': 'http://192.168.2.1',
        'username': 'admin', 'password': 'secret-password'})
    assert created.status_code == 200
    router_id = created.get_json()['router']['id']
    post = Mock(return_value=response(302, 'https://192.168.2.1/Action/login'))
    monkeypatch.setattr(requests.Session, 'post', post)
    result = client.post(f'/api/routers/{router_id}/probe', headers=admin)
    assert result.status_code == 502
    body = result.get_json()
    assert body['success'] is False
    assert '跳转到 https://192.168.2.1' in body['message']
    listed = client.get('/api/routers', headers=admin).get_json()['routers']
    assert listed[0]['last_probe']['message'] == body['message']
    post.assert_called_once()
