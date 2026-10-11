import os
from pathlib import Path
import tempfile

import pytest
import requests

# Nunca abrir la base, credenciales ni sesiones del servicio real durante las pruebas.
_directory = tempfile.TemporaryDirectory(prefix='inmobot-tests-')
os.environ.update(DATABASE_URL='sqlite:///' + str(Path(_directory.name) / 'test.db'),
                  APP_URL='https://inmobot.example', METRICAS_AUTO='0',
                  SECRET_KEY='test-only-secret', ALLOWED_IDS='123,456', PERFILES_JSON='[]')

import server
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('La prueba intentó usar la red real')
    monkeypatch.setattr(requests.sessions.Session, 'request', no_network)
    with server.SessionLocal() as db:
        for table in reversed(server.Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.add(server.Profile(telegram_id='123', nombre='Prueba', whatsapp='595981111111', token='test-key'))
        db.add(server.Profile(telegram_id='456', nombre='Otra cuenta', token='other-key'))
        db.commit()
    yield


@pytest.fixture
def client():
    with TestClient(server.app) as client:
        yield client


def pytest_unconfigure():
    server.engine.dispose()
    _directory.cleanup()
