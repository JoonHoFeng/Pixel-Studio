import asyncio
import base64
import io
import json
import os
from pathlib import Path
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms

from app import engine, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'STORE', tmp_path / 'store')
    with TestClient(main.app) as test_client:
        yield test_client


def picture(fmt='PNG', size=(40, 20), color=(240, 60, 80, 255), **kwargs):
    im = Image.new('RGBA', size, color)
    if fmt == 'JPEG':
        im = im.convert('RGB')
    buffer = io.BytesIO()
    im.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def upload(client, data=None, name='sample.png'):
    return client.post('/api/inspect', files={'file': (name, data or picture(), 'application/octet-stream')})


def convert(client, source, fmt='png', **kwargs):
    options = dict(image_id=source['image_id'], width=32, height=32, format=fmt)
    options.update(kwargs)
    return client.post('/api/convert', json=options)


def animation(fmt='GIF', loop=2):
    frames = [Image.new('RGBA', (20, 10), c) for c in ('red', 'blue', 'green')]
    buffer = io.BytesIO()
    args = dict(format=fmt, save_all=True, append_images=frames[1:], duration=[100, 200, 300], disposal=2)
    if loop is not None:
        args['loop'] = loop
    frames[0].save(buffer, **args)
    return buffer.getvalue()


def assert_error(response, code=None):
    assert response.status_code >= 400, response.text
    body = response.json()
    assert body['error']['message']
    if code:
        assert body['error']['code'] == code, body


def test_content_detection_and_false_extensions(client):
    data = upload(client, name='fake.jpg').json()
    assert data['format'] == 'PNG'
    assert data['width'] == 40 and data['warnings']
    assert client.get(data['preview_url']).headers['content-type'] == 'image/png'
    assert upload(client, picture('JPEG'), 'photo.bin').json()['format'] == 'JPEG'
    assert_error(upload(client, b'hello image', 'picture.png'), 'INVALID_IMAGE')
    assert_error(upload(client, b'\x00' * 100, 'camera.raw'), 'INVALID_IMAGE')


def test_corrupt_and_empty(client):
    assert_error(upload(client, picture()[:40]), 'INVALID_IMAGE')
    assert_error(upload(client, picture('JPEG')[:-15]), 'INVALID_IMAGE')
    response = client.post('/api/inspect', files={'file': ('empty.png', b'')})
    assert_error(response, 'EMPTY_FILE')
    assert not list(main.STORE.iterdir())


@pytest.mark.parametrize('fmt,expected', [('jpeg', 'JPEG'), ('jpg', 'JPEG'), ('png', 'PNG'), ('gif', 'GIF'), ('webp', 'WEBP'), ('tiff', 'TIFF'), ('tif', 'TIFF'), ('ico', 'ICO')])
def test_encoded_formats(client, fmt, expected):
    source = upload(client).json()
    response = convert(client, source, fmt, width=37, height=23)
    assert response.status_code == 200, response.text
    result = response.json()
    download = client.get(result['download_url'])
    assert download.status_code == 200
    with Image.open(io.BytesIO(download.content)) as im:
        im.load()
        assert im.format == expected and im.size == (37, 23)
        # TIFF always contains structural IFD tags (width, compression, etc.).
        assert not ({270, 271, 272, 274, 306, 315, 34853} & set(im.getexif()))
    assert 'attachment' in download.headers['content-disposition']
    assert client.get(result['preview_url']).status_code == 200


def test_raw_and_svg_roundtrip(client):
    source = upload(client).json()
    raw = convert(client, source, 'raw', width=21, height=19).json()
    with zipfile.ZipFile(io.BytesIO(client.get(raw['download_url']).content)) as archive:
        description = json.loads(archive.read('description.json'))
        assert description['channels'] == 'RGBA' and description['row_bytes'] == 84
        assert len(archive.read('pixels.raw')) == 21 * 19 * 4
    svg = convert(client, source, 'svg', width=23, height=27).json()
    data = client.get(svg['download_url']).content
    assert b'width="23"' in data and b'data:image/png;base64,' in data
    roundtrip = upload(client, data, 'output.svg')
    assert roundtrip.status_code == 200, roundtrip.text
    assert roundtrip.json()['format'] == 'SVG'
    assert (roundtrip.json()['width'], roundtrip.json()['height']) == (23, 27)


@pytest.mark.parametrize('markup', [
    '<script>alert(1)</script>', '<foreignObject/>', '<image href="file:///etc/passwd"/>',
    '<image href="https://example.com/x.png"/>', '<rect onclick="a()"/>',
    '<style>@import "https://example.com/style.css";</style>',
    '<rect style="fill:url(https://example.com/a)"/>',
    '<rect style="fill:u\\72l(\'file:///etc/passwd\')"/>',
    '<image href="data:image/svg+xml;base64,PHN2Zz4="/>', '<animate attributeName="x"/>',
    '<g xml:base="file:///tmp"/>',
])
def test_unsafe_svg(client, markup):
    data = f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">{markup}</svg>'.encode()
    assert_error(upload(client, data, 'fake.png'), 'UNSAFE_SVG')


def test_svg_entities_and_sizes(client):
    malicious = b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>'
    assert_error(upload(client, malicious, 'a.svg'), 'INVALID_SVG')
    assert_error(upload(client, b'<svg width="9000000" height="9000000"/>', 'a.svg'), 'PIXEL_LIMIT')
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40"><defs><linearGradient id="g"><stop stop-color="red"/></linearGradient></defs><rect width="80" height="40" fill="url(#g)"/></svg>'
    response = upload(client, svg, 'a.png')
    assert response.status_code == 200, response.text
    assert response.json()['format'] == 'SVG'
    assert response.json()['width'] == 80


@pytest.mark.parametrize('input_format', ['GIF', 'WEBP'])
@pytest.mark.parametrize('output_format', ['gif', 'webp'])
def test_animation(client, input_format, output_format):
    source = upload(client, animation(input_format), f'a.{input_format.lower()}').json()
    assert source['animated'] and source['frames'] == 3
    result = convert(client, source, output_format).json()
    data = client.get(result['download_url']).content
    with Image.open(io.BytesIO(data)) as im:
        expected_loop = 3 if (input_format, output_format) == ('GIF', 'webp') else 1 if (input_format, output_format) == ('WEBP', 'gif') else 2
        assert im.n_frames == 3 and im.info['loop'] == expected_loop
        durations = []
        for i in range(im.n_frames):
            im.seek(i)
            im.load()
            assert im.size == (32, 32)
            durations.append(im.info['duration'])
        assert durations == [100, 200, 300]
    static = convert(client, source, 'png').json()
    assert static['frames'] == 1 and any('首帧' in note for note in static['warnings'])


def test_no_loop_gif_stays_once(client):
    source = upload(client, animation(loop=None), 'once.gif').json()
    result = convert(client, source, 'gif').json()
    with Image.open(io.BytesIO(client.get(result['download_url']).content)) as im:
        assert 'loop' not in im.info


def test_fit_and_transparency():
    image = Image.new('RGBA', (40, 20), 'red')
    contained = engine.fit_image(image, (20, 20), 'contain', (0, 0, 0, 0))
    assert contained.getpixel((10, 0))[3] == 0
    assert contained.getpixel((10, 10)) == (255, 0, 0, 255)
    covered = engine.fit_image(image, (20, 20), 'cover', 'white')
    stretched = engine.fit_image(image, (20, 20), 'stretch', 'white')
    assert covered.getpixel((0, 0)) == stretched.getpixel((0, 0)) == (255, 0, 0, 255)


def test_alpha_jpeg_background_and_metadata(client):
    data = picture(color=(255, 0, 0, 0))
    source = upload(client, data).json()
    result = convert(client, source, 'jpeg').json()
    with Image.open(io.BytesIO(client.get(result['download_url']).content)) as im:
        assert all(x > 250 for x in im.getpixel((16, 16)))
    png = convert(client, source, 'png').json()
    with Image.open(io.BytesIO(client.get(png['download_url']).content)) as im:
        assert im.getpixel((16, 16))[3] == 0
    green = convert(client, source, 'png', background='#00ff00').json()
    with Image.open(io.BytesIO(client.get(green['download_url']).content)) as im:
        assert im.getpixel((16, 16)) == (0, 255, 0, 255)


def test_exif_and_color(client):
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'private metadata'
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    source = upload(client, picture('JPEG', exif=exif, icc_profile=profile), 'a.jpg').json()
    assert (source['width'], source['height']) == (20, 40)
    result = convert(client, source, 'jpeg').json()
    with Image.open(io.BytesIO(client.get(result['download_url']).content)) as im:
        assert not im.getexif() and 'icc_profile' not in im.info


def test_tiff_first_page(client):
    buffer = io.BytesIO()
    Image.new('RGB', (30, 10), 'red').save(buffer, format='TIFF', save_all=True, append_images=[Image.new('RGB', (10, 30), 'blue')])
    source = upload(client, buffer.getvalue(), 'a.tif').json()
    assert source['frames'] == 2 and source['warnings']
    result = convert(client, source, 'gif').json()
    assert result['frames'] == 1 and not result['animated']


def test_limits_and_parameters(client):
    source = upload(client).json()
    assert_error(convert(client, source, 'ico', width=257), 'ICO_SIZE')
    assert_error(convert(client, source, 'png', width=8000, height=8000), 'PIXEL_LIMIT')
    assert_error(convert(client, source, 'webp', width=16384, height=1), 'WEBP_SIZE')
    assert_error(convert(client, source, 'png', width=1.2), 'INVALID_OPTIONS')
    assert_error(convert(client, source, 'png', background='url(x)'), 'INVALID_OPTIONS')
    animated = upload(client, animation(), 'a.gif').json()
    assert_error(convert(client, animated, 'gif', width=6000, height=6000), 'TOTAL_PIXEL_LIMIT')


def test_input_resource_limits(monkeypatch):
    monkeypatch.setattr(engine, 'MAX_FRAMES', 2)
    with pytest.raises(engine.ImageError, match='300'):
        engine.decode(animation())
    monkeypatch.setattr(engine, 'MAX_FRAMES', 300)
    monkeypatch.setattr(engine, 'MAX_TOTAL', 300)
    with pytest.raises(engine.ImageError, match='1 亿'):
        engine.decode(animation())
    monkeypatch.setattr(engine, 'MAX_PIXELS', 100)
    with pytest.raises(engine.ImageError, match='4000 万'):
        engine.decode(picture())


def test_upload_size_and_origin(client, monkeypatch):
    monkeypatch.setattr(main, 'MAX_BYTES', 100)
    assert_error(upload(client, picture('JPEG')), 'FILE_TOO_LARGE')
    assert_error(client.post('/api/inspect', content=b'x' * (1024 * 1024 + 101)), 'FILE_TOO_LARGE')
    # Multipart without Content-Length still enforces the streaming body cap.
    multipart = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n' + b'x' * (1024 * 1024 + 101) + b'\r\n--b--\r\n'
    assert_error(client.post('/api/inspect', content=iter([multipart]), headers={'Content-Type': 'multipart/form-data; boundary=b'}), 'FILE_TOO_LARGE')
    assert_error(client.post('/api/inspect', headers={'Origin': 'https://evil.example'}), 'ORIGIN_REJECTED')


def test_worker_timeout_and_failed_cleanup(client, monkeypatch):
    monkeypatch.setattr(main, 'WORKER_TIMEOUT', .001)
    assert_error(upload(client), 'PROCESSING_TIMEOUT')
    assert not list(main.STORE.iterdir())


def test_expiry_and_cleanup(client):
    source = upload(client).json()
    folder = main.STORE / source['image_id']
    then = time.time() - main.TTL - 2
    os.utime(folder, (then, then))
    assert_error(client.get(source['preview_url']), 'EXPIRED')
    main.ACTIVE.add(source['image_id'])
    main.cleanup()
    assert folder.exists()
    main.ACTIVE.clear()
    main.cleanup()
    assert not folder.exists()
    assert_error(convert(client, source), 'EXPIRED')


def test_page_and_download_isolation(client):
    page = client.get('/')
    assert page.status_code == 200 and '像素工坊' in page.text
    assert 'nosniff' == page.headers['x-content-type-options']
    source = upload(client).json()
    assert_error(client.get(f'/api/files/{source["image_id"]}/download'), 'EXPIRED')
    assert client.get(f'/api/files/{source["image_id"]}/source').status_code == 422

@pytest.mark.parametrize('input_format,loop,output_format,expected', [
    ('GIF', None, 'webp', 1), ('GIF', 0, 'webp', 0),
    ('WEBP', 1, 'gif', None), ('WEBP', 0, 'gif', 0),
])
def test_animation_loop_semantics(client, input_format, loop, output_format, expected):
    source = upload(client, animation(input_format, loop=loop), 'a.' + input_format.lower()).json()
    result = convert(client, source, output_format).json()
    with Image.open(io.BytesIO(client.get(result['download_url']).content)) as im:
        assert im.info.get('loop') == expected


def test_center_crop_preserves_center_and_stretch_preserves_edges():
    image = Image.new('RGBA', (60, 20), 'red')
    image.paste('green', (20, 0, 40, 20))
    image.paste('blue', (40, 0, 60, 20))
    cover = engine.fit_image(image, (20, 20), 'cover', 'white')
    stretch = engine.fit_image(image, (60, 60), 'stretch', 'white')
    assert cover.getpixel((10, 10)) == (0, 128, 0, 255)
    assert cover.getpixel((0, 10))[1] > cover.getpixel((0, 10))[0]
    assert stretch.getpixel((5, 30)) == (255, 0, 0, 255)
    assert stretch.getpixel((55, 30)) == (0, 0, 255, 255)


def test_corrupt_later_animation_frame_rejected(client):
    data = animation()
    assert_error(upload(client, data[:-12], 'a.gif'), 'INVALID_IMAGE')


def test_moving_transparent_frames_do_not_leave_trails(client):
    frames = []
    for x in [0, 8, 16]:
        frame = Image.new('RGBA', (24, 8), (0, 0, 0, 0))
        frame.paste('red', (x, 0, x + 8, 8))
        frames.append(frame)
    stream = io.BytesIO()
    frames[0].save(stream, format='GIF', save_all=True, append_images=frames[1:], duration=100, loop=0, disposal=2)
    source = upload(client, stream.getvalue(), 'moving.gif').json()
    result = convert(client, source, 'gif', width=24, height=8).json()
    with Image.open(io.BytesIO(client.get(result['download_url']).content)) as im:
        for i in range(3):
            im.seek(i)
            frame = im.convert('RGBA')
            assert frame.getpixel((i * 8 + 4, 4))[3] == 255
            assert frame.getpixel((((i + 1) % 3) * 8 + 4, 4))[3] == 0
