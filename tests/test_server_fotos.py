import json

import pytest
from fastapi.testclient import TestClient

import fotos
import server
from seguridad import bot_key
from publicador import ResultadoIncierto
from test_fotos import image_bytes


def prepare(monkeypatch, count=7, bad=False):
    urls = [f'https://photos.example/{i}.jpg' for i in range(count)]
    monkeypatch.setattr(server, 'extraer_propiedad', lambda url: {
        'url': url, 'title': 'Casa en venta en Luque', 'price': 'USD 120000',
        'description': 'Casa con 3 dormitorios', 'images': urls, 'images_found': count})
    def read(url):
        index = int(url.rsplit('/', 1)[-1].split('.')[0])
        if bad and index == 1:
            raise ValueError('Descarga fallida')
        return image_bytes(index), url
    monkeypatch.setattr(fotos, 'leer_publico', read)


def create(client):
    response = client.post('/api/drafts', json={'telegram_id': 123,
        'source_url': 'https://portal.example/property', 'raw_data': {}}, headers={'X-Bot-Key': bot_key()})
    assert response.status_code == 200, response.text
    return response.json()['draft_id']


def request_publish(client, ident, **extra):
    return client.post('/api/publish', json={'telegram_id': 123, 'k': 'test-key',
        'draft_id': ident, 'destination': 'fb_feed', **extra})


def connected(pages=None):
    with server.SessionLocal() as db:
        db.add(server.FbUser(telegram_id='123', pages_json=json.dumps(pages or
               [{'id': 'page', 'name': 'Página de prueba', 'access_token': 'test-token'}])))
        db.commit()


def test_link_to_saved_files_and_full_dashboard_survive_new_connection(client, monkeypatch):
    prepare(monkeypatch)
    ident = create(client)
    server.engine.dispose()
    with TestClient(server.app) as another:
        result = another.get('/api/dashboard', params={'tid': '123', 'k': 'test-key'}).json()
        draft = result['avisos'][0]
        assert len(draft['images']) == draft['n_fotos'] == 7
        assert draft['photos_saved'] and not draft['photos_incomplete']
        assert result['perfil']['login_url'].endswith('k=test-key')
        for image in draft['images']:
            response = another.get(image)
            assert response.status_code == 200
            assert response.headers['content-type'] == 'image/jpeg'
            assert response.content[:2] == b'\xff\xd8'
    with server.SessionLocal() as db:
        assert db.query(server.DraftPhoto).filter_by(draft_id=ident).count() == 7


def test_failed_photo_blocks_partial_publication(client, monkeypatch):
    prepare(monkeypatch, count=3, bad=True)
    ident = create(client)
    response = request_publish(client, ident)
    assert response.status_code == 409 and 'Faltan fotos' in response.json()['detail']


def test_one_old_photo_is_completed_from_original_link_without_changing_text(client, monkeypatch):
    prepare(monkeypatch)
    with server.SessionLocal() as db:
        db.add(server.Draft(id='legacy', telegram_id='123', source_url='https://portal.example/property',
                            images_json='["https://portal.example/cover.jpg"]', copy_fb='Texto editado'))
        db.commit()
    assert request_publish(client, 'legacy').status_code == 409
    response = client.post('/api/drafts/legacy/photos/refresh', json={'telegram_id': 123, 'k': 'test-key'})
    assert response.status_code == 200 and response.json()['n_fotos'] == 7
    with server.SessionLocal() as db:
        draft = db.get(server.Draft, 'legacy')
        assert len(json.loads(draft.images_json)) == 7
        assert draft.copy_fb == 'Texto editado'


def test_other_user_cannot_refresh_or_delete_gallery(client, monkeypatch):
    prepare(monkeypatch)
    ident = create(client)
    assert client.post(f'/api/drafts/{ident}/photos/refresh', json={'telegram_id': 456, 'k': 'other-key'}).status_code == 404
    assert client.delete(f'/api/drafts/{ident}?telegram_id=456&k=other-key').status_code == 404
    with server.SessionLocal() as db:
        assert db.query(server.DraftPhoto).filter_by(draft_id=ident).count() == 7


def test_successful_retry_does_not_publish_same_network_twice(client, monkeypatch):
    prepare(monkeypatch); ident = create(client); connected()
    uploads = []
    def publish(page, token, photos, caption, before):
        assert len(photos) == 7
        before(); uploads.append(photos); return 'post-id'
    monkeypatch.setattr(server, 'publicar_facebook', publish)
    first = request_publish(client, ident)
    second = request_publish(client, ident)
    assert first.json()['success'] and second.json()['already_published']
    assert len(uploads) == 1


def test_uncertain_final_request_is_remembered_across_retry(client, monkeypatch):
    prepare(monkeypatch); ident = create(client); connected()
    def publish(page, token, photos, caption, before):
        before(); raise ResultadoIncierto('Revisar la cuenta')
    monkeypatch.setattr(server, 'publicar_facebook', publish)
    assert request_publish(client, ident).json()['requires_review']
    assert request_publish(client, ident).status_code == 409
    assert client.delete(f'/api/drafts/{ident}?telegram_id=123&k=test-key').status_code == 409


def test_page_choice_required_when_multiple_pages(client, monkeypatch):
    prepare(monkeypatch); ident = create(client)
    connected([{'id': p, 'name': p, 'access_token': 'test-token'} for p in ['a', 'b']])
    assert request_publish(client, ident).status_code == 400
    selected = []
    def publish(page, token, photos, caption, before):
        selected.append(page); before(); return 'post-id'
    monkeypatch.setattr(server, 'publicar_facebook', publish)
    assert request_publish(client, ident, page_id='b').json()['success']
    assert selected == ['b']


def test_delete_removes_stored_files(client, monkeypatch):
    prepare(monkeypatch); ident = create(client)
    assert client.delete(f'/api/drafts/{ident}?telegram_id=123&k=test-key').status_code == 200
    with server.SessionLocal() as db:
        assert db.query(server.DraftPhoto).count() == 0


def test_preview_only_source_is_not_marked_as_complete(client, monkeypatch):
    prepare(monkeypatch, count=1)
    extract = server.extraer_propiedad
    monkeypatch.setattr(server, 'extraer_propiedad', lambda url: {
        **extract(url), 'gallery_warning': 'No se pudo confirmar la galería completa.'})
    ident = create(client)
    assert request_publish(client, ident).status_code == 409


def test_instagram_uses_all_saved_public_images_and_saves_final_caption(client, monkeypatch):
    prepare(monkeypatch); ident = create(client); connected()
    monkeypatch.setattr(server, 'validar_url_publica', lambda url: url)
    uploaded = []
    def publish(page, token, urls, caption, before):
        assert len(urls) == 7
        assert all(u.startswith('https://inmobot.example/media/') and u.endswith('?version=instagram') for u in urls)
        for url in urls:
            assert client.get(url).status_code == 200
        assert caption == 'Texto revisado'
        before(); uploaded.extend(urls); return 'ig-post-id'
    monkeypatch.setattr(server, 'publicar_instagram', publish)
    response = request_publish(client, ident, destination='ig_feed', texto='Texto revisado')
    assert response.json()['success'] and len(uploaded) == 7
    with server.SessionLocal() as db:
        assert db.get(server.Draft, ident).copy_ig == 'Texto revisado'
        assert db.query(server.Publicacion).filter_by(draft_id=ident, destino='ig_feed').count() == 1


def test_existing_database_migration_preserves_old_drafts(monkeypatch, tmp_path):
    from sqlalchemy import create_engine, text, inspect
    from sqlalchemy.orm import sessionmaker
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as con:
        con.execute(text('CREATE TABLE drafts (id VARCHAR PRIMARY KEY, telegram_id VARCHAR, title TEXT, images_json TEXT, copy_fb TEXT)'))
        con.execute(text("INSERT INTO drafts VALUES ('old', '123', 'Aviso existente', '[\"https://example.com/cover.jpg\"]', 'Texto original')"))
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(server, 'engine', engine)
    monkeypatch.setattr(server, 'SessionLocal', factory)
    try:
        server.migrar()
        server.migrar()  # El siguiente arranque tampoco borra ni duplica datos.
        assert 'draft_photos' in inspect(engine).get_table_names()
        with factory() as db:
            draft = db.get(server.Draft, 'old')
            assert draft.title == 'Aviso existente' and draft.copy_fb == 'Texto original'
            assert len(json.loads(draft.images_json)) == 1
            assert not server._json(draft.photos_meta_json, {}).get('saved')
    finally:
        engine.dispose()
