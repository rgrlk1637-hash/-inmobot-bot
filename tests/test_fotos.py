import io
import json
import socket

import pytest
from PIL import Image

import fotos


def image_bytes(index=0, size=(480, 320), fmt='JPEG'):
    image = Image.new('RGB', size, (20 + index * 17, 40 + index * 7, 60 + index * 5))
    stream = io.BytesIO()
    image.save(stream, fmt)
    return stream.getvalue()


@pytest.mark.parametrize('count,expected', [(1, 1), (3, 3), (7, 7), (10, 10), (14, 10)])
def test_downloads_every_photo_in_order_up_to_ten(monkeypatch, count, expected):
    urls = [f'https://photos.example/{i}.jpg' for i in range(count)]
    downloads = []
    def read(url):
        downloads.append(url)
        return image_bytes(int(url.split('/')[-1].split('.')[0])), url
    monkeypatch.setattr(fotos, 'leer_publico', read)
    result, warnings, incomplete = fotos.descargar_fotos(urls)
    assert len(result) == expected
    assert [p['source_url'] for p in result] == urls[:expected]
    assert len(downloads) == expected
    assert not incomplete
    for p in result:
        assert p['original']
        for key in ('jpeg', 'instagram'):
            with Image.open(io.BytesIO(p[key])) as image:
                assert image.format == 'JPEG'
    assert bool(warnings) == (count > 10)


def test_deduplicates_urls_and_actual_files_then_fills_limit(monkeypatch):
    urls = ['https://photos.example/same.jpg'] * 2 + ['https://photos.example/copy.jpg']
    urls += [f'https://photos.example/{i}.jpg' for i in range(1, 12)]
    def read(url):
        name = url.rsplit('/', 1)[-1].split('.')[0]
        return image_bytes(0 if name in ('same', 'copy') else int(name)), url
    monkeypatch.setattr(fotos, 'leer_publico', read)
    result, _, incomplete = fotos.descargar_fotos(urls)
    assert len(result) == 10
    assert len({p['sha256'] for p in result}) == 10
    assert not incomplete


def test_corrupt_photo_is_reported_not_silently_complete(monkeypatch):
    monkeypatch.setattr(fotos, 'leer_publico', lambda u: (b'<html>denied</html>' if 'bad' in u else image_bytes(), u))
    result, warnings, incomplete = fotos.descargar_fotos(['https://x.example/good.jpg', 'https://x.example/bad.jpg'])
    assert len(result) == 1 and incomplete
    assert 'No se pudieron descargar 1' in warnings[0]


def test_different_shapes_are_padded_without_losing_edges(monkeypatch):
    wide = Image.new('RGB', (800, 400), 'blue')
    tall = Image.new('RGB', (200, 600), 'red')
    for x in range(200):
        for y in range(12):
            tall.putpixel((x, y), (0, 255, 0))
            tall.putpixel((x, 599-y), (0, 0, 255))
    data = []
    for image in (wide, tall):
        out = io.BytesIO(); image.save(out, 'PNG'); data.append(out.getvalue())
    monkeypatch.setattr(fotos, 'leer_publico', lambda u: (data[int(u[-1])], u))
    photos, _, _ = fotos.descargar_fotos(['https://x.example/0', 'https://x.example/1'])
    with Image.open(io.BytesIO(photos[0]['instagram'])) as first, Image.open(io.BytesIO(photos[1]['instagram'])) as second:
        assert first.size == second.size
        assert second.getpixel((second.width//2, 2))[1] > 200
        assert second.getpixel((second.width//2, second.height-3))[2] > 200
        assert second.getpixel((0, second.height//2)) == (255, 255, 255)


def test_c21_gallery_not_cover():
    data = {'entity': {'id': 123, 'enInternet': True, 'encabezado': 'Casa en venta',
                      'tipoOperacion': 'venta', 'moneda': 'USD', 'precioVenta': 120000},
            'fotos': [{'large2': f'https://cdn.21online.lat/{i}.jpg'} for i in range(7)]}
    parsed = fotos.parse_c21(data, '123')
    assert len(parsed['images']) == 7
    assert parsed['price'] == 'USD 120000'
    with pytest.raises(ValueError):
        fotos.parse_c21(data, '999')


def test_remax_order_and_watermark():
    e = {'MLSID': '123-4', 'IsViewable': True, 'RegionId': 114,
         'ListingImages': [{'Order': 2, 'FileName': 'b.jpg', 'IsWatermarked': 1},
                           {'Order': 1, 'FileName': 'a.jpg', 'IsWatermarked': 0}]}
    parsed = fotos.parse_remax({'value': [{'content': e}]}, '123-4')
    assert parsed['images'] == ['https://cdn.gryphtech.com/userimages/114/Large/a.jpg',
                                'https://cdn.gryphtech.com/userimages/114/LargeWM/b.jpg']


def test_generic_gallery_lazy_images_relative_paths_not_logos():
    html = '''<meta property="og:image" content="/cover.jpg"><img src="/logo.jpg">
      <div class="property-gallery"><img data-src="/1.jpg"><a href="/2.jpg"><img src="/2.jpg"></a>
      <img srcset="/small.jpg 300w, /3.jpg 1200w"></div><aside><img src="/other-house.jpg"></aside>'''
    parsed = fotos.parse_html(html, 'https://portal.example/listing')
    assert parsed['images'] == [f'https://portal.example/{i}.jpg' for i in (1, 2, 3)]


def test_jsonld_ignores_organization_and_person_images():
    graph = [{'@type': 'Organization', 'image': '/logo.jpg'}, {'@type': 'Person', 'image': '/agent.jpg'},
             {'@type': 'House', 'image': ['/one.jpg', {'contentUrl': '/two.jpg'}]}]
    html = '<script type="application/ld+json">' + json.dumps({'@graph': graph}) + '</script>'
    assert fotos.parse_html(html, 'https://portal.example/')['images'] == ['https://portal.example/one.jpg', 'https://portal.example/two.jpg']


def test_thumbnail_link_uses_full_photo_only():
    html = '<div class="gallery"><a href="/full.jpg"><img src="/thumb.jpg"></a></div>'
    assert fotos.parse_html(html, 'https://portal.example/')['images'] == ['https://portal.example/full.jpg']


def test_empty_dynamic_gallery_does_not_hide_cover_only_warning():
    html = '<meta property="og:image" content="/cover.jpg"><div class="gallery"></div>'
    parsed = fotos.parse_html(html, 'https://portal.example/')
    assert parsed['images'] == ['https://portal.example/cover.jpg']
    assert parsed['gallery_warning']


def test_explicit_gallery_with_one_photo_is_valid():
    html = '<div class="gallery"><img src="/one.jpg"></div>'
    assert not fotos.parse_html(html, 'https://portal.example/')['gallery_warning']


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/a', 'http://u:p@example.com',
                               'http://127.0.0.1/photo', 'http://169.254.169.254/', 'http://[::1]/'])
def test_rejects_internal_destinations(monkeypatch, url):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, 1, 6, '', ('127.0.0.1', 80))])
    with pytest.raises(ValueError):
        fotos.validar_url_publica(url)
