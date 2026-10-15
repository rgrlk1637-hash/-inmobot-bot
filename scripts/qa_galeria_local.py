"""Prueba del enlace al panel con una base aislada. Nunca publica en redes."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument('url')
parser.add_argument('--salida', type=Path, default=ROOT / 'analisis' / 'qa-galeria' / 'integracion')
args = parser.parse_args()
out = args.salida.resolve()
out.mkdir(parents=True, exist_ok=True)
os.environ.update(DATABASE_URL='sqlite:///' + str(out / 'qa.db'), APP_URL='http://inmobot.test',
                  SECRET_KEY='qa-local-only', ALLOWED_IDS='123', PERFILES_JSON='[]', METRICAS_AUTO='0')

from fastapi.testclient import TestClient
import server
from seguridad import bot_key

with server.SessionLocal() as db:
    if not db.get(server.Profile, '123'):
        db.add(server.Profile(telegram_id='123', token='test-key', nombre='Prueba local', whatsapp='595981111111'))
        db.commit()
with TestClient(server.app) as client:
    response = client.post('/api/drafts', json={'telegram_id': 123, 'source_url': args.url, 'raw_data': {}},
                           headers={'X-Bot-Key': bot_key()})
    response.raise_for_status()
    result = response.json()
    dashboard = client.get('/api/dashboard?tid=123&k=test-key').json()
    dashboard['avisos'] = [d for d in dashboard['avisos'] if d['id'] == result['draft_id']]
    (out / 'dashboard.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'media').mkdir(exist_ok=True)
    for url in dashboard['avisos'][0]['images']:
        photo = client.get(url)
        photo.raise_for_status()
        (out / 'media' / url.rsplit('/', 1)[-1]).write_bytes(photo.content)
    summary = {'url': args.url, 'fotos_guardadas': result['n_fotos'], 'base_aislada': str(out / 'qa.db'),
               'avisos': result['photo_warnings'], 'publicaciones_reales': 0}
    (out / 'resultado.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
server.engine.dispose()
