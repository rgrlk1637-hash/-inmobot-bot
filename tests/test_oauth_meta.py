import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

import server
from seguridad import firmar_state

LARGO = 5184000  # 60 dias


class Respuesta:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


@pytest.fixture
def meta(monkeypatch):
    """Meta simulada: registra cada llamada y responde segun el recurso pedido."""
    monkeypatch.setattr(server, 'FB_APP_ID', 'app-id')
    monkeypatch.setattr(server, 'FB_SECRET', 'app-secret')
    server._ESTADO_META.clear()
    estado = {'llamadas': [], 'valido': True, 'vence': 0}

    def get(url, params=None, timeout=None):
        params = params or {}
        estado['llamadas'].append((url.rsplit('/', 1)[-1] if 'me/accounts' not in url else 'me/accounts', params))
        if url.endswith('oauth/access_token') and 'code' in params:
            return Respuesta({'access_token': 'corto', 'expires_in': 3600})
        if url.endswith('oauth/access_token'):
            assert params['grant_type'] == 'fb_exchange_token' and params['fb_exchange_token'] == 'corto'
            return Respuesta({'access_token': 'largo', 'expires_in': LARGO})
        if url.endswith('me/accounts'):
            assert params['access_token'] == 'largo'
            return Respuesta({'data': [{'id': 'page', 'name': 'Elvio Brun Bienes Raíces', 'access_token': 'token-pagina'}]})
        if url.endswith('debug_token'):
            return Respuesta({'data': {'is_valid': estado['valido'], 'expires_at': 0,
                                       'data_access_expires_at': estado['vence']}})
        raise AssertionError(url)

    monkeypatch.setattr(server.requests, 'get', get)
    yield estado
    server._ESTADO_META.clear()


def dashboard(client):
    return client.get('/api/dashboard', params={'tid': '123', 'k': 'test-key'}).json()['perfil']


def test_privacy_page_for_meta_publication(client):
    response = client.get('/privacidad')
    assert response.status_code == 200 and 'id=\'eliminar\'' in response.text


def test_login_url_encodes_return_address(client, monkeypatch):
    monkeypatch.setattr(server, 'FB_APP_ID', 'app-id')
    response = client.get('/auth/login', params={'telegram_id': '123', 'k': 'test-key'}, follow_redirects=False)
    query = parse_qs(urlparse(response.headers['location']).query)
    assert query['redirect_uri'] == ['https://inmobot.example/auth/callback']
    assert 'instagram_content_publish' in query['scope'][0]


def test_callback_stores_long_lived_token_and_expiry(client, meta):
    vence = int((server.ahora() + timedelta(days=90)).timestamp())
    meta['vence'] = vence
    response = client.get('/auth/callback', params={'code': 'abc', 'state': firmar_state('123', 'n1')})
    assert response.status_code == 200 and 'Elvio Brun Bienes Raíces' in response.text
    with server.SessionLocal() as db:
        user = db.query(server.FbUser).filter_by(telegram_id='123').one()
        assert user.access_token == 'largo'
        assert json.loads(user.pages_json)[0]['access_token'] == 'token-pagina'
        assert int(server.local(user.vence_at).timestamp()) == vence
    perfil = dashboard(client)
    assert perfil['fb_conectado'] and perfil['fb_estado'] == 'conectado' and perfil['fb_vence']


def test_cancelled_or_forged_callback_does_not_call_meta(client, meta):
    cancelado = client.get('/auth/callback', params={'state': firmar_state('123', 'n1'), 'error': 'access_denied'})
    falso = client.get('/auth/callback', params={'state': '123|n1|firma-falsa', 'code': 'abc'})
    assert cancelado.status_code == falso.status_code == 200
    assert 'No se completó' in cancelado.text and 'no válido' in falso.text
    assert meta['llamadas'] == []


def test_dashboard_offers_reconnect_when_meta_revokes_token(client, meta):
    with server.SessionLocal() as db:
        db.add(server.FbUser(telegram_id='123', access_token='largo', pages_json=json.dumps(
            [{'id': 'page', 'name': 'Página', 'access_token': 'token-pagina'}])))
        db.commit()
    meta['valido'] = False
    perfil = dashboard(client)
    assert not perfil['fb_conectado'] and perfil['fb_estado'] == 'vencido'
    dashboard(client)
    assert [c[0] for c in meta['llamadas']] == ['debug_token']  # se consulta como maximo cada 30 min


def test_expired_connection_blocks_publication_without_calling_meta(client, monkeypatch):
    with server.SessionLocal() as db:
        db.add(server.FbUser(telegram_id='123', access_token='largo', vence_at=server.ahora() - timedelta(days=1),
                             pages_json=json.dumps([{'id': 'page', 'name': 'Página', 'access_token': 'token-pagina'}])))
        db.add(server.Draft(id='d1', telegram_id='123', title='Casa',
                            photos_meta_json=json.dumps({'saved': True, 'downloaded': 1})))
        db.commit()
    response = client.post('/api/publish', json={'telegram_id': 123, 'k': 'test-key',
                                                 'draft_id': 'd1', 'destination': 'fb_feed'})
    assert response.status_code == 400 and 'venció' in response.json()['detail']
    assert dashboard(client)['fb_estado'] == 'vencido'


def test_callback_shows_meta_reason_when_code_exchange_fails(client, monkeypatch, capsys):
    monkeypatch.setattr(server, 'FB_APP_ID', 'app-id')
    monkeypatch.setattr(server, 'FB_SECRET', 'app-secret')
    monkeypatch.setattr(server.requests, 'get', lambda url, params=None, timeout=None: Respuesta(
        {'error': {'message': 'Error validating client secret.', 'type': 'OAuthException', 'code': 1}}))
    response = client.get('/auth/callback', params={'code': 'abc', 'state': firmar_state('123', 'n1')})
    assert 'Error validating client secret. (código 1)' in response.text
    assert 'app-secret' not in response.text
    assert 'canje del código: Error validating client secret.' in capsys.readouterr().out
