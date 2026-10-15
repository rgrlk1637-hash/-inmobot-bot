"""Publicaciones de una o varias fotos. No publica parcialmente una galería."""
import json
import os
import time

import requests

GRAPH_VERSION = os.getenv('META_GRAPH_VERSION', 'v26.0')
GRAPH = f'https://graph.facebook.com/{GRAPH_VERSION}'


class ErrorPublicacion(ValueError):
    pass


class ResultadoIncierto(ErrorPublicacion):
    """No repetir automáticamente una publicación que Meta pudo haber aceptado."""


def _llamar(method, path, token, final=False, **kwargs):
    try:
        response = requests.request(method, f'{GRAPH}/{path}',
                                    headers={'Authorization': f'Bearer {token}'},
                                    timeout=(8, 45), **kwargs)
        if final and response.status_code >= 500:
            raise ResultadoIncierto('Meta no confirmó el resultado. Revisá la cuenta antes de volver a publicar.')
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Respuesta no válida')
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, ErrorPublicacion):
            raise
        message = 'No se pudo confirmar la respuesta de Meta.'
        if final:
            raise ResultadoIncierto(message + ' Revisá la cuenta antes de volver a publicar.') from exc
        raise ErrorPublicacion(message) from exc
    if not response.ok or result.get('error'):
        raise ErrorPublicacion((result.get('error') or {}).get('message') or 'Meta rechazó la solicitud.')
    return result


def _id(result, final=False):
    ident = result.get('post_id') or result.get('id')
    if not ident:
        error = ResultadoIncierto if final else ErrorPublicacion
        raise error('Meta no devolvió el identificador de la publicación.' if final else
                    'Meta no devolvió el identificador de una foto. No se publicó el aviso.')
    return str(ident)


def publicar_facebook(page_id, token, photos, caption, antes_de_publicar=lambda: None):
    if len(photos) > 10:
        raise ErrorPublicacion('El límite es de 10 fotos por publicación.')
    attached = []
    for photo in photos:
        result = _llamar('POST', f'{page_id}/photos', token, data={'published': 'false'},
                         files={'source': ('foto.jpg', photo, 'image/jpeg')})
        attached.append({'media_fbid': _id(result)})
    data = {'message': caption}
    if attached:
        data['attached_media'] = json.dumps(attached)
    # Recién después de cargar TODAS las fotos se crea una única publicación.
    antes_de_publicar()
    return _id(_llamar('POST', f'{page_id}/feed', token, final=True, data=data), final=True)


def _esperar(ids, token, timeout=300, intervalo=60):
    deadline = time.monotonic() + timeout
    pending = set(ids)
    while pending:
        for ident in tuple(pending):
            result = _llamar('GET', ident, token, params={'fields': 'status_code,status'})
            status = result.get('status_code')
            if status == 'FINISHED':
                pending.remove(ident)
            elif status in ('ERROR', 'EXPIRED', 'PUBLISHED'):
                raise ErrorPublicacion('Instagram no pudo preparar todas las fotos. No se publicó el aviso.')
            elif status != 'IN_PROGRESS':
                raise ErrorPublicacion('Instagram no confirmó que las fotos estén listas.')
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ErrorPublicacion('Instagram sigue preparando las fotos. Podés reintentar más tarde.')
        time.sleep(min(intervalo, remaining))


def publicar_instagram(page_id, token, urls, caption, antes_de_publicar=lambda: None):
    if not 1 <= len(urls) <= 10:
        raise ErrorPublicacion('Instagram necesita entre 1 y 10 fotos.')
    account = _llamar('GET', page_id, token, params={'fields': 'instagram_business_account'})
    ig_id = (account.get('instagram_business_account') or {}).get('id')
    if not ig_id:
        raise ErrorPublicacion('La página no tiene una cuenta profesional de Instagram vinculada.')
    children = []
    for url in urls:
        data = {'image_url': url}
        if len(urls) > 1:
            data['is_carousel_item'] = 'true'
        else:
            data['caption'] = caption
        children.append(_id(_llamar('POST', f'{ig_id}/media', token, data=data)))
    _esperar(children, token)
    container = children[0]
    if len(children) > 1:
        container = _id(_llamar('POST', f'{ig_id}/media', token, data={
            'media_type': 'CAROUSEL', 'children': ','.join(children), 'caption': caption}))
        _esperar([container], token)
    antes_de_publicar()
    return _id(_llamar('POST', f'{ig_id}/media_publish', token, final=True,
                      data={'creation_id': container}), final=True)
