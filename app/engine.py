"""Content-driven image validation and conversion. Runs only in disposable workers."""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
import re
import warnings
import zipfile
import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
import resvg_py
import tinycss2

MAX_BYTES = 30 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_TOTAL = 100_000_000
MAX_FRAMES = 300
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
warnings.simplefilter('error', Image.DecompressionBombWarning)
RASTER = {'JPEG', 'PNG', 'GIF', 'WEBP', 'TIFF', 'ICO'}
ALIASES = {'jpg': 'jpeg', 'tif': 'tiff'}
SVG_NS = 'http://www.w3.org/2000/svg'
SAFE_TAGS = set('svg g defs title desc metadata path rect circle ellipse line polyline polygon text tspan textPath use symbol clipPath mask pattern linearGradient radialGradient stop marker filter feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feImage feMerge feMergeNode feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence image style switch'.split())


class ImageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def check_size(size):
    w, h = size
    if w < 1 or h < 1 or w * h > MAX_PIXELS:
        raise ImageError('PIXEL_LIMIT', '图片宽高必须为正数，且总像素不能超过 4000 万。')


def srgb_frame(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    profile = im.info.get('icc_profile')
    alpha = im.convert('RGBA').getchannel('A')
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            color = im if im.mode in ('RGB', 'CMYK', 'LAB', 'L') else im.convert('RGB')
            rgb = ImageCms.profileToProfile(color, source, ImageCms.createProfile('sRGB'), outputMode='RGB')
            out = rgb.convert('RGBA')
            out.putalpha(alpha)
        except Exception as exc:
            raise ImageError('INVALID_COLOR_PROFILE', '图片色彩配置损坏或不受支持，请使用有效的色彩配置重新导出。') from exc
    else:
        out = im.convert('RGBA')
    out.info.clear()
    return out


def _css(tokens):
    for token in tokens:
        if token.type == 'error':
            raise ImageError('UNSAFE_SVG', 'SVG 样式无法安全解析。')
        if token.type == 'at-keyword':
            raise ImageError('UNSAFE_SVG', 'SVG 不支持外部样式或 CSS @ 规则。')
        if token.type == 'url':
            if not re.fullmatch(r'#[\w.:-]+', token.value):
                raise ImageError('UNSAFE_SVG', 'SVG 不能引用外部资源。')
        if token.type == 'function':
            if token.lower_name == 'url':
                args = [t for t in token.arguments if t.type not in ('whitespace', 'comment')]
                if len(args) != 1 or args[0].type != 'string' or not re.fullmatch(r'#[\w.:-]+', args[0].value):
                    raise ImageError('UNSAFE_SVG', 'SVG 不能引用外部资源。')
            elif token.lower_name not in ('rgb', 'rgba', 'hsl', 'hsla', 'matrix', 'translate', 'scale', 'rotate', 'skewx', 'skewy', 'calc'):
                raise ImageError('UNSAFE_SVG', 'SVG 包含不支持的样式函数。')
            _css(token.arguments)
        if hasattr(token, 'content'):
            _css(token.content)


def svg_raster(data: bytes) -> bytes:
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as exc:
        raise ImageError('INVALID_SVG', 'SVG XML 无效，或包含不允许的实体/文档类型。') from exc
    if root.tag not in ('svg', f'{{{SVG_NS}}}svg'):
        raise ImageError('UNSUPPORTED_FORMAT', '文件内容不是受支持的图片。RAW 原始像素缺少描述信息，无法自动识别。')
    count, embedded_pixels = 0, 0
    for node in root.iter():
        count += 1
        if count > 10_000:
            raise ImageError('SVG_LIMIT', 'SVG 元素过多，最多支持 10000 个元素。')
        tag = node.tag.split('}')[-1]
        if tag not in SAFE_TAGS or (node.tag.startswith('{') and not node.tag.startswith('{' + SVG_NS + '}')):
            raise ImageError('UNSAFE_SVG', 'SVG 包含脚本、动画或不支持的元素。')
        if tag == 'style':
            _css(tinycss2.parse_component_value_list(node.text or ''))
        for key, value in list(node.attrib.items()):
            attr = key.split('}')[-1].lower()
            if attr.startswith('on') or attr in ('base', 'src'):
                raise ImageError('UNSAFE_SVG', 'SVG 不允许事件处理或外部资源。')
            if attr == 'href':
                if re.fullmatch(r'#[\w.:-]+', value):
                    continue
                match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)', value)
                if tag not in ('image', 'feImage') or not match:
                    raise ImageError('UNSAFE_SVG', 'SVG 只允许内部引用和内嵌 PNG、JPEG、WebP 图片。')
                try:
                    content = base64.b64decode(re.sub(r'\s', '', match[2]), validate=True)
                    frames, meta = decode_raster(content, keep_frames=True)
                    if meta['format'] not in ('PNG', 'JPEG', 'WEBP') or meta['frames'] != 1:
                        raise ValueError('invalid embedded image')
                    embedded_pixels += frames[0].width * frames[0].height
                    if embedded_pixels > MAX_TOTAL:
                        raise ImageError('PIXEL_LIMIT', 'SVG 内嵌图片累计像素超限。')
                    encoded = io.BytesIO()
                    frames[0].save(encoded, format='PNG')
                    node.set(key, 'data:image/png;base64,' + base64.b64encode(encoded.getvalue()).decode())
                except ImageError:
                    raise
                except Exception as exc:
                    raise ImageError('INVALID_SVG', 'SVG 内嵌图片无效。') from exc
            else:
                _css(tinycss2.parse_component_value_list(value))
    view = re.split(r'[\s,]+', root.get('viewBox', '').strip())
    box = None
    if len(view) == 4:
        try:
            box = list(map(float, view))
            if not all(math.isfinite(x) for x in box) or box[2] <= 0 or box[3] <= 0:
                raise ValueError()
        except ValueError as exc:
            raise ImageError('INVALID_SVG', 'SVG viewBox 必须包含有效尺寸。') from exc
    def length(value, fallback):
        if value is None or value.endswith('%'):
            return fallback
        match = re.fullmatch(r'\s*(\d+(?:\.\d*)?|\.\d+)\s*(px|pt|pc|in|cm|mm)?\s*', value)
        if not match:
            raise ImageError('INVALID_SVG', 'SVG 宽高必须为有效的正数长度。')
        return float(match[1]) * {'px': 1, 'pt': 96 / 72, 'pc': 16, 'in': 96, 'cm': 96 / 2.54, 'mm': 96 / 25.4, None: 1}[match[2]]
    w = length(root.get('width'), box[2] if box else 300)
    h = length(root.get('height'), box[3] if box else 150)
    if not math.isfinite(w) or not math.isfinite(h) or w <= 0 or h <= 0:
        raise ImageError('INVALID_SVG', 'SVG 尺寸无效。')
    size = (math.ceil(w), math.ceil(h))
    check_size(size)
    root.set('width', str(size[0]))
    root.set('height', str(size[1]))
    try:
        return resvg_py.svg_to_bytes(svg_string=ET.tostring(root, encoding='unicode'), width=size[0], height=size[1])
    except Exception as exc:
        raise ImageError('INVALID_SVG', 'SVG 无法渲染。') from exc


def decode_raster(data: bytes, keep_frames=False):
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            if fmt not in RASTER:
                raise ImageError('UNSUPPORTED_FORMAT', '仅支持 JPEG、PNG、GIF、WebP、TIFF、ICO 和 SVG 图片输入。')
            check_size(probe.size)
            probe.verify()
        frames, durations = [], []
        with Image.open(io.BytesIO(data)) as im:
            loop = im.info.get('loop')
            count, total = 0, 0
            # Do not use n_frames: scan with bounded seeks, validating every frame.
            while True:
                if count >= MAX_FRAMES:
                    raise ImageError('FRAME_LIMIT', '图片最多支持 300 帧或页面。')
                check_size(im.size)
                total += im.width * im.height
                if total > MAX_TOTAL:
                    raise ImageError('TOTAL_PIXEL_LIMIT', '图片累计像素超过 1 亿。')
                im.load()
                duration = max(0, int(im.info.get('duration', 100)))
                durations.append(duration)
                if count == 0 or keep_frames:
                    frames.append(srgb_frame(im))
                count += 1
                try:
                    im.seek(count)
                except EOFError:
                    break
            meta = {'format': fmt, 'width': frames[0].width, 'height': frames[0].height,
                    'frames': count, 'durations': durations, 'loop': loop,
                    'animated': count > 1 and fmt in ('GIF', 'WEBP', 'PNG')}
        return frames, meta
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageError('PIXEL_LIMIT', '图片解码尺寸超出安全限制。') from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageError('INVALID_IMAGE', '文件不是有效图片，或图片已损坏/截断。不能凭后缀识别 RAW 原始像素。') from exc


def decode(data: bytes, keep_frames=False):
    # XML is parsed, not trusted merely because a .svg extension was supplied.
    prefix = data.lstrip(b'\xef\xbb\xbf\x00\t\r\n ')
    if prefix.startswith(b'<') or data.startswith((b'\xff\xfe', b'\xfe\xff')):
        png = svg_raster(data)
        frames, meta = decode_raster(png)
        meta['format'] = 'SVG'
        return frames, meta
    return decode_raster(data, keep_frames)


def save_preview(frame, path):
    frame = frame.copy()
    frame.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    frame.info.clear()
    frame.save(path, format='PNG')


def inspect_image(source: str, filename: str, folder: str):
    frames, meta = decode(Path(source).read_bytes())
    ext = ALIASES.get(Path(filename).suffix.lower().lstrip('.'), Path(filename).suffix.lower().lstrip('.'))
    notes = []
    if ext != meta['format'].lower():
        notes.append(f'文件后缀与内容不一致：实际格式为 {meta["format"]}，将按真实格式处理。')
    if meta['format'] == 'TIFF' and meta['frames'] > 1:
        notes.append('这是多页 TIFF，转换时仅使用第一页。')
    if meta['format'] == 'SVG':
        notes.append('SVG 已通过安全检查，预览为栅格化图像。')
    meta.update(size=Path(source).stat().st_size, warnings=notes)
    save_preview(frames[0], Path(folder) / 'preview.png')
    return meta


def fit_image(frame, size, mode, background):
    if mode == 'stretch':
        content = frame.resize(size, Image.Resampling.LANCZOS)
    elif mode == 'cover':
        content = ImageOps.fit(frame, size, Image.Resampling.LANCZOS, centering=(.5, .5))
    else:
        content = ImageOps.contain(frame, size, Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', size, background)
    canvas.alpha_composite(content, ((size[0] - content.width) // 2, (size[1] - content.height) // 2))
    return canvas


def gif_frame(frame):
    # GIF has binary transparency; reserve index 255 rather than quantizing it away.
    alpha = frame.getchannel('A')
    quantized = frame.convert('RGB').quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    quantized.putpalette((quantized.getpalette() + [0] * 768)[:765] + [0, 0, 0])
    mask = alpha.point(lambda a: 255 if a < 128 else 0)
    quantized.paste(255, mask=mask)
    quantized.info['transparency'] = 255
    return quantized


def convert_image(source: str, options: dict, folder: str):
    fmt = ALIASES.get(options['format'].lower(), options['format'].lower())
    size = (options['width'], options['height'])
    check_size(size)
    if fmt == 'ico' and max(size) > 256:
        raise ImageError('ICO_SIZE', 'ICO 的宽高都必须在 1–256 像素之间。')
    if fmt == 'webp' and max(size) > 16383:
        raise ImageError('WEBP_SIZE', 'WebP 的单边尺寸不能超过 16383 像素。')
    # Read the already validated metadata before retaining potentially many frames.
    metadata = json.loads((Path(source).parent / 'meta.json').read_text())
    animate = metadata['animated'] and fmt in ('gif', 'webp')
    output_count = metadata['frames'] if animate else 1
    if size[0] * size[1] * output_count > MAX_TOTAL:
        raise ImageError('TOTAL_PIXEL_LIMIT', '动画输出累计像素超过 1 亿，请减小尺寸。')
    frames, meta = decode(Path(source).read_bytes(), keep_frames=animate)
    notes = []
    if meta['animated'] and not animate:
        notes.append('当前输出为静态图片，仅使用动图首帧。')
    if meta['format'] == 'TIFF' and meta['frames'] > 1:
        notes.append('多页 TIFF 仅使用第一页。')
    if not animate:
        frames = frames[:1]
    bg = options['background']
    if bg == 'transparent':
        bg = '#ffffff' if fmt == 'jpeg' else (0, 0, 0, 0)
        if fmt == 'jpeg':
            notes.append('JPEG 不支持透明，透明区域已使用白色填充。')
    output = [fit_image(f, size, options['fit'], bg) for f in frames]
    del frames
    folder = Path(folder)
    ext = {'jpeg': 'jpg', 'tiff': 'tif', 'raw': 'zip'}.get(fmt, fmt)
    path = folder / f'result.{ext}'
    kwargs = {}
    if fmt in ('jpeg', 'webp'):
        kwargs['quality'] = options['quality']
    if fmt == 'jpeg':
        output = [output[0].convert('RGB')]
    if fmt == 'webp':
        kwargs.update(lossless=options['lossless'], exact=True)
    if fmt == 'gif':
        notes.append('GIF 使用最多 256 色和二值透明，颜色及半透明边缘可能发生变化。')
        output = [gif_frame(f) for f in output]
        kwargs.update(transparency=255, disposal=2, optimize=False)
    if animate:
        durations = meta['durations']
        if fmt == 'gif' and any(d % 10 for d in durations):
            durations = [max(10, round(d / 10) * 10) if d else 0 for d in durations]
            notes.append('GIF 的时长精度为 10 毫秒，帧时长已取最近可表示值。')
        kwargs.update(save_all=True, append_images=output[1:], duration=durations)
        # GIF counts repetitions after the first play; WebP/APNG count total plays.
        loop = meta['loop']
        plays = (1 if loop is None else 0 if loop == 0 else loop + 1) if meta['format'] == 'GIF' else (loop if loop is not None else 1)
        if fmt == 'webp':
            if plays > 65535:
                raise ImageError('LOOP_LIMIT', '源动画循环次数超过 WebP 可表示范围。')
            kwargs['loop'] = plays
        elif plays != 1:
            kwargs['loop'] = 0 if plays == 0 else plays - 1
    if fmt == 'ico':
        kwargs['sizes'] = [size]
    if fmt == 'tiff':
        kwargs['compression'] = 'tiff_deflate'
    if fmt == 'raw':
        spec = {'width': size[0], 'height': size[1], 'channels': 'RGBA', 'bits_per_channel': 8,
                'layout': 'interleaved', 'row_order': 'top-to-bottom', 'pixel_order': 'left-to-right',
                'row_bytes': size[0] * 4, 'color_space': 'sRGB', 'alpha': 'straight', 'data_file': 'pixels.raw'}
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('pixels.raw', output[0].tobytes())
            archive.writestr('description.json', json.dumps(spec, ensure_ascii=False, indent=2))
        notes.append('RAW 为 8 位 RGBA 原始像素及 JSON 说明的 ZIP 包，不是相机 RAW。')
    elif fmt == 'svg':
        png = io.BytesIO()
        output[0].save(png, format='PNG')
        data = base64.b64encode(png.getvalue()).decode()
        path.write_text(f'<svg xmlns="{SVG_NS}" width="{size[0]}" height="{size[1]}" viewBox="0 0 {size[0]} {size[1]}"><image width="{size[0]}" height="{size[1]}" href="data:image/png;base64,{data}"/></svg>')
        notes.append('SVG 内嵌 PNG 位图，非矢量描摹。')
    else:
        output[0].save(path, format=fmt.upper(), **kwargs)
    # Preview the encoded result so palette/quality changes are visible.
    if fmt in ('raw', 'svg'):
        preview = output[0].convert('RGBA')
    else:
        with Image.open(path) as encoded:
            encoded.load()
            preview = encoded.convert('RGBA')
            actual_count = getattr(encoded, 'n_frames', 1)
            if animate and actual_count != output_count:
                notes.append('编码器已合并相同画面，结果帧数减少。')
                output_count = actual_count
                animate = actual_count > 1
    save_preview(preview, folder / 'preview.png')
    return {'format': fmt.upper(), 'width': size[0], 'height': size[1], 'frames': output_count,
            'size': path.stat().st_size, 'extension': ext, 'warnings': notes, 'animated': animate}
