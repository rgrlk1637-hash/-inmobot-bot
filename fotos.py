"""Galerías públicas y descarga validada de las fotos de una propiedad."""
import hashlib
import io
import ipaddress
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_FOTOS = 10
MAX_CANDIDATAS = 80
MAX_BYTES = 16_000_000
Image.MAX_IMAGE_PIXELS = 40_000_000


def validar_url_publica(url):
    try:
        u = urlsplit(url)
        if (u.scheme not in ('http', 'https') or not u.hostname or u.username
                or u.password or u.port not in (None, 80, 443)):
            raise ValueError
        addresses = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80),
                                       type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError
    except (ValueError, TypeError, OSError) as exc:
        raise ValueError('El enlace debe apuntar a una página pública de Internet.') from exc
    return urlunsplit((u.scheme, u.netloc, u.path, u.query, ''))


def leer_publico(url, metodo='GET', cuerpo=None, max_bytes=MAX_BYTES):
    """Limita tiempos, tamaño y redirecciones; rechaza destinos internos."""
    inicio = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(6):
            url = validar_url_publica(url)
            try:
                with session.request(metodo, url, json=cuerpo, timeout=(6, 20), stream=True,
                                     allow_redirects=False,
                                     headers={'User-Agent': 'Mozilla/5.0', 'Accept': '*/*'}) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get('Location', ''))
                        if response.status_code == 303:
                            metodo, cuerpo = 'GET', None
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    for part in response.iter_content(65536):
                        content.extend(part)
                        if len(content) > max_bytes or time.monotonic() - inicio > 45:
                            raise ValueError('La descarga supera el tamaño o el tiempo permitido.')
                    return bytes(content), url
            except requests.RequestException as exc:
                raise ValueError('No se pudo descargar la página o la foto del portal.') from exc
    raise ValueError('El enlace redirige demasiadas veces.')


def _unicas(urls, base):
    out, seen = [], set()
    for value in urls:
        if not isinstance(value, str) or not value.strip():
            continue
        url = urljoin(base, value.strip().replace('\\/', '/'))
        u = urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            continue
        url = urlunsplit((u.scheme, u.netloc, u.path, u.query, ''))
        # No quitar parámetros: algunos portales identifican cada foto en el query.
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _texto(value):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)


def _precio(value, moneda):
    if value is None or value == '':
        return ''
    try:
        return f'{moneda} {float(value):.2f}'.removesuffix('.00')
    except (ValueError, TypeError):
        return ''


def parse_c21(data, ident):
    e = data['entity']
    if str(e.get('id')) != ident or not e.get('enInternet'):
        raise ValueError('La propiedad ya no está disponible públicamente.')
    fotos = [f for f in data.get('fotos', []) if f.get('tipo') not in ('360', 'panoramica')]
    amount = e.get('precioVenta') if e.get('tipoOperacion') == 'venta' else e.get('precioRenta')
    return {'title': e.get('encabezado', 'Propiedad'), 'description': _texto(e.get('descripcion')),
            'price': '' if e.get('ocultarPrecioInternet') else _precio(amount, e.get('moneda', '')),
            'images': [f.get('large2') or f.get('large') or f.get('url') for f in fotos],
            'portal': 'CENTURY 21'}


def parse_remax(data, ident):
    records = data.get('value', [])
    if len(records) != 1:
        raise ValueError('No se encontró una única propiedad para ese enlace.')
    e = records[0]['content']
    if e.get('MLSID') != ident or not e.get('IsViewable') or e.get('OnHoldListing'):
        raise ValueError('La propiedad ya no está disponible públicamente.')
    descriptions = [d for d in e.get('ListingDescriptions', []) if d.get('ISOLanguageCode') == 'es']
    title = next((d.get('Description') for d in descriptions if str(d.get('DescriptionTypeUID')) == '1113'), '')
    body = next((d.get('Description') for d in descriptions if str(d.get('DescriptionTypeUID')) == '629'), '')
    images = []
    for f in sorted(e.get('ListingImages', []), key=lambda f: int(f.get('Order') or 0)):
        folder = 'LargeWM' if str(f.get('IsWatermarked')) == '1' else 'Large'
        images.append(f"https://cdn.gryphtech.com/userimages/{e['RegionId']}/{folder}/{f['FileName']}")
    return {'title': _texto(title), 'description': _texto(body),
            'price': '' if e.get('HidePricePublic') else _precio(e.get('ListingPrice'), e.get('ListingCurrency', '')),
            'images': images, 'portal': 'RE/MAX'}


def _image_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [url for item in value for url in _image_values(item)]
    if isinstance(value, dict):
        for key in ('contentUrl', 'original', 'large', 'url', 'src'):
            if value.get(key):
                return _image_values(value[key])
    return []


def parse_html(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    def meta(name):
        tag = soup.find('meta', property=name) or soup.find('meta', attrs={'name': name})
        return tag.get('content', '') if tag else ''
    images = []
    # Datos estructurados del inmueble. Nunca recoger logos/avatares de Organization/Person.
    types = {'RealEstateListing', 'Residence', 'House', 'Apartment', 'Product', 'SingleFamilyResidence',
             'Accommodation', 'ApartmentComplex', 'Place', 'Offer'}
    def structured(node):
        if isinstance(node, list):
            for item in node:
                structured(item)
        elif isinstance(node, dict):
            kind = node.get('@type', [])
            kind = [kind] if isinstance(kind, str) else kind
            if set(kind or []) & types:
                images.extend(_image_values(node.get('image', [])))
                images.extend(_image_values(node.get('photo', [])))
            for key in ('@graph', 'mainEntity', 'itemOffered'):
                if key in node:
                    structured(node[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            structured(json.loads(script.get_text()))
        except (ValueError, TypeError):
            pass
    # Galerías explícitas; no recorrer todas las imágenes de la página (avisos relacionados, logos).
    roots = soup.select('[class*="gallery" i], [id*="gallery" i], [class*="galeria" i], '
                        '[id*="galeria" i], [data-gallery], [class*="property-slider" i]')
    for root in roots:
        for tag in root.select('img, a, source'):
            if tag.name == 'a' and tag.find('img'):
                continue
            if tag.name == 'source' and tag.parent.name == 'picture' and tag.parent.find('img'):
                continue
            value = next((tag.get(k) for k in ('data-full', 'data-original', 'data-large', 'data-src', 'data-lazy-src')
                          if tag.get(k)), None)
            parent = tag.find_parent('a') if tag.name == 'img' else None
            if not value and parent and re.search(r'\.(jpe?g|png|webp)(?:[?#]|$)', parent.get('href', ''), re.I):
                value = parent['href']
            if not value and tag.get('srcset'):
                choices = [p.strip().split() for p in tag['srcset'].split(',') if p.strip()]
                if choices:
                    value = choices[-1][0]
            value = value or tag.get('src') or tag.get('href')
            if tag.name == 'a' and not re.search(r'\.(jpe?g|png|webp)(?:[?#]|$)', value or '', re.I):
                continue
            if value:
                images.append(value)
    gallery_found = bool(_unicas(images, url))
    if not gallery_found:
        images = [t.get('content') for t in soup.select('meta[property="og:image"]')]
    price = meta('product:price:amount')
    if price:
        price = f"{meta('product:price:currency')} {price}"
    else:
        match = re.search(r'(USD|U\$S|US\$|\$|Gs\.)\s?[\d.,]+', soup.get_text(' ', strip=True))
        price = match.group(0) if match else ''
    return {'title': meta('og:title') or (soup.title.get_text() if soup.title else 'Propiedad'),
            'description': meta('og:description') or meta('description'), 'price': price,
            'images': _unicas(images, url), 'portal': urlsplit(url).hostname,
            'gallery_warning': '' if gallery_found else
            'El sitio solo expuso su vista previa. No se pudo confirmar la galería completa.'}


def extraer_propiedad(url):
    url = validar_url_publica(url)
    u = urlsplit(url)
    host = u.hostname.lower().removeprefix('www.')
    if host == 'century21.com.py':
        match = re.match(r'/propiedad/(\d+)', u.path)
        if not match:
            raise ValueError('Usá el enlace de una propiedad de CENTURY 21, no una página de búsqueda.')
        data, _ = leer_publico(f'https://century21.com.py/propiedad/{match[1]}?json=true')
        result = parse_c21(json.loads(data), match[1])
    elif host == 'remax.com.py':
        match = re.search(r'/(\d+-\d+)/?$', u.path)
        if not match:
            raise ValueError('Usá el enlace de una propiedad de RE/MAX, no una página de búsqueda.')
        query = {'skip': 0, 'top': 1, 'searchMode': 'any', 'queryType': 'simple', 'search': '*',
                 'filter': f"content/TenantId eq 6 and content/RegionId eq 114 and content/MLSID eq '{match[1]}' and content/OnHoldListing eq false and content/IsViewable eq true"}
        data, _ = leer_publico('https://www.remax.com.py/search/listing-search/docs/search', 'POST', query)
        result = parse_remax(json.loads(data), match[1])
    else:
        html, url = leer_publico(url)
        result = parse_html(html, url)
    result['url'] = url
    result['images'] = _unicas(result.get('images', []), url)
    result['images_found'] = len(result['images'])
    result['images'] = result['images'][:MAX_CANDIDATAS]
    if not result['images']:
        raise ValueError('No se encontró una galería descargable en ese enlace. El portal puede requerir una lectura específica.')
    return result


def _abrir_imagen(content):
    try:
        with Image.open(io.BytesIO(content)) as source:
            if (source.format not in ('JPEG', 'PNG', 'WEBP') or min(source.size) < 160
                    or source.width * source.height > 20_000_000):
                raise ValueError('La foto es demasiado pequeña o tiene un formato no admitido.')
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                background = Image.new('RGBA', image.size, 'white')
                background.alpha_composite(rgba)
                return background.convert('RGB')
            return image.convert('RGB')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('El enlace no devolvió una foto válida.') from exc


def _jpeg(image):
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=90, optimize=True)
    return out.getvalue()


def _descargar(url):
    try:
        content, _ = leer_publico(url)
        image = _abrir_imagen(content)
        width, height = image.size
        # Compara píxeles además de URL: no repetir una misma foto con metadatos diferentes.
        digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        return {'source_url': url, 'original': content, 'jpeg': _jpeg(image),
                'width': width, 'height': height, 'sha256': digest}, None
    except (ValueError, OSError) as exc:
        return None, str(exc)


def descargar_fotos(urls, source_url=''):
    candidates = _unicas(urls, source_url)[:MAX_CANDIDATAS]
    photos, seen, errors = [], set(), []
    index = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        while len(photos) < MAX_FOTOS and index < len(candidates):
            batch = candidates[index:index + MAX_FOTOS - len(photos)]
            index += len(batch)
            # map conserva el orden original aunque las descargas terminen en otro orden.
            for photo, error in pool.map(_descargar, batch):
                if error:
                    errors.append(error)
                elif photo['sha256'] not in seen:
                    seen.add(photo['sha256'])
                    photos.append(photo)
    if not photos:
        raise ValueError('No se pudo descargar ninguna foto válida del enlace. Probá actualizar las fotos más tarde.')
    first = photos[0]
    ratio = max(4 / 5, min(1.91, first['width'] / first['height']))
    canvas_size = (1080, round(1080 / ratio))
    for photo in photos:
        image = _abrir_imagen(photo['jpeg'])
        fitted = ImageOps.contain(image, canvas_size, Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', canvas_size, 'white')
        canvas.paste(fitted, ((canvas.width - fitted.width) // 2, (canvas.height - fitted.height) // 2))
        photo['instagram'] = _jpeg(canvas)
    incomplete = bool(errors) and len(photos) < MAX_FOTOS
    warnings = []
    if incomplete:
        warnings.append(f'No se pudieron descargar {len(errors)} foto(s). Actualizá las fotos antes de publicar.')
    if len(candidates) > MAX_FOTOS:
        warnings.append(f'El enlace contiene {len(candidates)} fotos; se usarán hasta {MAX_FOTOS}, sin repetirlas.')
    return photos, warnings, incomplete
