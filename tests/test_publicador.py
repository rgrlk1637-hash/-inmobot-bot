import json

import pytest
import requests

import publicador


@pytest.mark.parametrize('count', [1, 3, 7, 10])
def test_facebook_uploads_all_then_exactly_one_post(monkeypatch, count):
    calls = []
    def api(method, path, token, **kwargs):
        calls.append((method, path, kwargs))
        return {'id': f'id{len(calls)}'}
    monkeypatch.setattr(publicador, '_llamar', api)
    result = publicador.publicar_facebook('page', 'test-token', [bytes([i]) for i in range(count)], 'Texto')
    assert len(calls) == count + 1 and result
    assert all(p == 'page/photos' and k['data']['published'] == 'false' for m, p, k in calls[:-1])
    assert [c[2]['files']['source'][1] for c in calls[:-1]] == [bytes([i]) for i in range(count)]
    assert calls[-1][1] == 'page/feed'
    assert len(json.loads(calls[-1][2]['data']['attached_media'])) == count


def test_facebook_failure_in_middle_never_publishes_partial_album(monkeypatch):
    calls = []
    def api(method, path, token, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise publicador.ErrorPublicacion('Foto rechazada')
        return {'id': 'uploaded'}
    monkeypatch.setattr(publicador, '_llamar', api)
    with pytest.raises(publicador.ErrorPublicacion):
        publicador.publicar_facebook('page', 'test-token', [b'1', b'2', b'3'], 'Texto')
    assert 'page/feed' not in calls


@pytest.mark.parametrize('count', [1, 3, 10])
def test_instagram_children_and_final_publish_after_ready(monkeypatch, count):
    calls = []
    def api(method, path, token, **kwargs):
        calls.append((method, path, kwargs))
        if method == 'GET' and path == 'page':
            return {'instagram_business_account': {'id': 'ig'}}
        if method == 'GET':
            return {'status_code': 'FINISHED'}
        return {'id': f'id{len(calls)}'}
    monkeypatch.setattr(publicador, '_llamar', api)
    urls = [f'https://app.example/{i}.jpg' for i in range(count)]
    publicador.publicar_instagram('page', 'test-token', urls, 'Texto')
    creates = [c[2]['data'] for c in calls if c[0] == 'POST' and c[1] == 'ig/media']
    assert [c['image_url'] for c in creates if 'image_url' in c] == urls
    if count == 1:
        assert creates == [{'image_url': urls[0], 'caption': 'Texto'}]
    else:
        assert all(c['is_carousel_item'] == 'true' for c in creates[:-1])
        assert creates[-1]['media_type'] == 'CAROUSEL'
        assert len(creates[-1]['children'].split(',')) == count
    assert calls[-1][1] == 'ig/media_publish'
    assert sum(c[1] == 'ig/media_publish' for c in calls) == 1


def test_instagram_failed_child_does_not_publish(monkeypatch):
    paths = []
    def api(method, path, token, **kwargs):
        paths.append(path)
        if path == 'page':
            return {'instagram_business_account': {'id': 'ig'}}
        if method == 'GET':
            return {'status_code': 'ERROR'}
        return {'id': 'container'}
    monkeypatch.setattr(publicador, '_llamar', api)
    with pytest.raises(publicador.ErrorPublicacion):
        publicador.publicar_instagram('page', 'test-token', ['https://app.example/1.jpg'], 'Texto')
    assert 'ig/media_publish' not in paths


def test_waiting_container_times_out_without_publishing(monkeypatch):
    monkeypatch.setattr(publicador, '_llamar', lambda *a, **k: {'status_code': 'IN_PROGRESS'})
    with pytest.raises(publicador.ErrorPublicacion, match='sigue preparando'):
        publicador._esperar(['container'], 'test-token', timeout=0)


def test_uncertain_final_request_must_not_be_automatically_retried(monkeypatch):
    def timeout(*a, **k):
        raise requests.Timeout()
    monkeypatch.setattr(requests, 'request', timeout)
    with pytest.raises(publicador.ResultadoIncierto):
        publicador._llamar('POST', 'page/feed', 'test-token', final=True, data={})
