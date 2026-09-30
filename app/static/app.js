'use strict';
const $ = id => document.getElementById(id);
let source = null, result = null, busy = false, stale = false, version = 0;
const hints = {
  png: '无损保存，适合截图、图形和透明图片。',
  jpeg: '通用照片格式；不支持透明，默认使用白色背景。',
  webp: '支持透明与动画，可选择有损或无损压缩。',
  gif: '最多 256 色，支持动画；半透明会转换为二值透明。',
  svg: '将位图嵌入 SVG 容器，不会描摹为矢量路径。',
  tiff: '使用无损压缩；多页 TIFF 输入仅转换第一页。',
  ico: '单尺寸图标，宽高必须各为 1–256 像素。',
  raw: '下载 ZIP：8 位 RGBA 原始像素 + JSON 说明，不是相机 RAW。'
};
function bytes(n) { return n >= 1048576 ? `${(n / 1048576).toFixed(2)} MB` : `${(n / 1024).toFixed(1)} KB`; }
function status(message, type = '') { $('status').textContent = message; $('status').dataset.type = type; }
function warnings(items) { $('warnings').replaceChildren(...items.map(text => { const li = document.createElement('li'); li.textContent = text; return li; })); $('warnings').hidden = !items.length; }
function lock(value) { busy = value; $('options').disabled = value || !source; $('replace').disabled = value; $('dropzone').disabled = value; $('generate').textContent = value ? '处理中…' : '生成图片 ↗'; }
function showPreview(which) {
  const generated = which === 'result' && result && !stale;
  if (!source) return;
  $('preview').src = generated ? result.preview_url : source.preview_url;
  $('preview').alt = generated ? '生成结果预览' : '原图预览';
  $('original-tab').setAttribute('aria-pressed', String(!generated));
  $('result-tab').setAttribute('aria-pressed', String(!!generated));
  $('preview-label').textContent = generated ? (result.animated ? '动画预览' : '生成结果 · 适应窗口') : (source.animated ? '原图首帧 · 适应窗口' : '原图 · 适应窗口');
}
function invalidate() {
  version++;
  if (!result) return;
  stale = true;
  $('result').classList.add('stale');
  $('result-title').textContent = '设置已修改，请重新生成';
  $('result-badge').textContent = '待更新';
  $('download').removeAttribute('href');
  $('download').setAttribute('aria-disabled', 'true');
  $('download').tabIndex = -1;
  $('result-tab').disabled = true;
  showPreview('original');
  status('旧结果已失效，点击“生成图片”应用当前设置。');
}
function formatState() {
  const format = $('format').value;
  $('format-hint').textContent = hints[format];
  $('quality-area').hidden = !['jpeg', 'webp'].includes(format);
  $('lossless-area').hidden = format !== 'webp';
  $('quality').disabled = format === 'webp' && $('lossless').checked;
  $('color').disabled = $('background-mode').value !== 'color';
  $('bg-hint').textContent = $('background-mode').value === 'color' ? $('color').value.toUpperCase() : format === 'jpeg' ? 'JPEG 将使用白色' : '保留透明区域';
}
async function api(url, options) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 125000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || '请求失败，请重试。');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求等待超时，请稍后重试。');
    if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认启动窗口仍在运行。');
    throw error;
  } finally { clearTimeout(timeout); }
}
async function upload(file) {
  if (busy || !file) return;
  if (file.size > 30 * 1024 * 1024) { status('文件超过 30 MB 上传限制。', 'error'); return; }
  if (!file.size) { status('上传文件为空。', 'error'); return; }
  lock(true); status('正在识别真实格式并校验图片内容…', 'busy');
  const body = new FormData(); body.append('file', file);
  try {
    const data = await api('/api/inspect', { method: 'POST', body });
    source = data; result = null; stale = false; version++;
    $('dropzone').hidden = true; $('image-workspace').hidden = false; $('replace').hidden = false; $('result').hidden = true;
    $('filename').textContent = data.filename;
    $('file-meta').textContent = `${bytes(data.size)} · ${data.animated ? '动态图片' : '本地图片'}`;
    $('detected').textContent = data.format; $('dimensions').textContent = `${data.width} × ${data.height}`; $('frames').textContent = data.frames;
    $('width').value = data.width; $('height').value = data.height;
    $('result-tab').disabled = true; showPreview('original'); warnings(data.warnings);
    status(`校验通过，已识别为 ${data.format}。设置输出参数后即可生成。`);
  } catch (error) { status(error.message, 'error'); }
  finally { lock(false); $('file').value = ''; formatState(); }
}
$('dropzone').addEventListener('click', () => $('file').click());
$('replace').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', event => upload(event.target.files[0]));
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, e => { e.preventDefault(); if (!busy) $('dropzone').classList.add('dragover'); });
for (const event of ['dragleave', 'drop']) $('dropzone').addEventListener(event, e => { e.preventDefault(); $('dropzone').classList.remove('dragover'); });
$('dropzone').addEventListener('drop', e => { if (e.dataTransfer.files.length !== 1) return status('每次请选择一张图片。', 'error'); upload(e.dataTransfer.files[0]); });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());
$('original-tab').addEventListener('click', () => showPreview('original'));
$('result-tab').addEventListener('click', () => showPreview('result'));
$('settings').addEventListener('input', () => { $('quality-value').textContent = $('quality').value; formatState(); invalidate(); });
$('reset-size').addEventListener('click', () => { if (!source) return; $('width').value = source.width; $('height').value = source.height; invalidate(); });
$('settings').addEventListener('submit', async e => {
  e.preventDefault(); if (!source || busy) return;
  const width = Number($('width').value), height = Number($('height').value), format = $('format').value;
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 40000000) return status('请输入正整数宽高，总像素不能超过 4000 万。', 'error');
  if (format === 'ico' && Math.max(width, height) > 256) return status('ICO 的宽高都必须在 1–256 像素之间。', 'error');
  if (format === 'webp' && Math.max(width, height) > 16383) return status('WebP 的单边尺寸不能超过 16383 像素。', 'error');
  lock(true); status('正在生成图片，请稍候…', 'busy');
  const requestVersion = version;
  const options = { image_id: source.image_id, width, height, format, fit: document.querySelector('input[name="fit"]:checked').value, background: $('background-mode').value === 'transparent' ? 'transparent' : $('color').value, quality: Number($('quality').value), lossless: $('lossless').checked };
  try {
    const data = await api('/api/convert', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options) });
    if (requestVersion !== version) return;
    result = data; stale = false; $('result').hidden = false; $('result').classList.remove('stale');
    $('result-title').textContent = '图片已就绪'; $('result-badge').textContent = 'READY';
    $('result-meta').textContent = `${data.format} · ${data.width} × ${data.height} px · ${bytes(data.size)}${data.animated ? ` · ${data.frames} 帧` : ''}`;
    $('download').href = data.download_url; $('download').download = data.filename; $('download').removeAttribute('aria-disabled'); $('download').removeAttribute('tabindex');
    $('download').textContent = format === 'raw' ? '下载 RAW 压缩包 ↓' : '下载图片 ↓';
    $('result-tab').disabled = false; showPreview('result'); warnings([...source.warnings, ...data.warnings]);
    status('生成成功，可预览并下载。结果将在 30 分钟后过期。');
  } catch (error) { status(error.message, 'error'); }
  finally { lock(false); formatState(); }
});
$('preview').addEventListener('error', () => { if (source) status('预览无法加载，临时文件可能已过期，请重新上传。', 'error'); });
formatState();
