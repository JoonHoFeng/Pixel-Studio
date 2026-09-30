# 像素工坊

中文本地图片转换工具。上传一张图片，验证真实格式，调整尺寸并生成 JPEG、PNG、SVG、GIF、WebP、RAW、TIFF 或 ICO。前端无第三方资源，图片不发送到第三方服务。

## 启动

需要 Python 3.10 或更新版本（已在 macOS / Python 3.14 验证）。首次安装依赖需要联网；安装完成后可离线使用。

macOS：双击 **启动.command**。也可以在本目录执行：

```sh
./start.sh
```

打开 <http://127.0.0.1:8765>。服务仅监听本机，终端按 `Ctrl+C` 停止。端口已被占用时先停止旧进程。启动脚本会创建 `.venv` 并安装固定依赖；不需要 Node.js、系统 Cairo 或 ImageMagick。

Windows 手动启动：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## 使用

1. 拖入一张图片或点击选择文件。检查显示的真实格式、原始尺寸、帧数和校验提示。
2. 输入像素宽高，选择等比留白、居中裁剪或直接拉伸。默认等比留白、透明背景。
3. 选择输出格式，按需设置背景色与 JPEG/WebP 质量（默认 90）。
4. 点击“生成图片”，切换原图/结果预览并下载。修改参数后旧结果不可下载，需要重新生成。

## 格式约定

| 格式 | 输入 / 输出行为 |
| --- | --- |
| JPEG / JPG | 内容识别；输出有损 RGB，透明背景默认白色 |
| PNG | 输出静态无损 RGBA；APNG 输入也会逐帧检查 |
| GIF | GIF/WebP 动画输入导出 GIF 时保留时序；最多 256 色，透明度以 128 为阈值转换为二值透明 |
| WebP | 支持有损、无损、透明及动画；宽高各最多 16383 像素 |
| SVG | 安全解析后由 resvg 栅格化输入；输出是内嵌 PNG 的 SVG，**不是矢量描摹** |
| TIFF / TIF | 完整检查各页，仅将第一页用于输出；输出单页 Deflate 无损压缩 TIFF |
| ICO | 输入默认使用最大图标；输出一个指定尺寸，宽高各为 1–256 像素 |
| RAW | 仅输出 ZIP：`pixels.raw` + `description.json`；**不是相机 RAW**。无描述信息的原始像素不能自动识别，拒绝输入 |

动图导出静态格式时仅取首帧并提示。GIF/WebP 支持动画输出，PNG 输出为静态。转换后相同的连续帧可能被编码器合并，并保留合并帧的时长（若全部合并为单张，则输出静态图片）。无循环扩展的 GIF 只播放一次；跨格式转换会换算循环字段，保持浏览器中的播放次数，GIF 的帧时长精度为 10 毫秒。SVG 和 RAW 使用位图预览；动画结果直接播放生成的 GIF/WebP。

照片会应用 EXIF 方向。有有效 ICC 配置的图片转换到 sRGB；无配置图片按 sRGB 解释。导出移除源 EXIF、GPS 等元数据，不保留印刷专用色彩信息。JPEG 与 GIF 属于有损/限色输出。

## 内容校验与限制

- 使用解码器探测内容，重新完整解码所有帧；后缀与 MIME 不作为可信判断依据。后缀错误但内容有效时允许转换并提示。
- 对任意不支持的内容、解码失败、截断文件返回稳定错误码与中文错误。真实格式识别并非取证级文件审计：不声称检测一切附加尾随数据或多格式文件。
- SVG 禁止 DTD、实体、脚本、事件、动画、foreignObject、外部资源、CSS `@` 规则和未知元素。支持常见图形、文字、渐变、滤镜和内部片段引用；只接受内嵌 PNG/JPEG/WebP，检查后重新编码。复杂 SVG 可能因严格规则被拒绝。
- 上传最多 30 MiB；单帧/输出画布最多 4000 万像素；最多 300 帧或页；输入与输出累计各最多 1 亿像素；SVG 最多 10000 个元素。
- 每个任务运行于独立进程，30 秒墙钟超时后强制终止，同时最多运行两个任务。进程隔离不是完整操作系统沙箱。Linux 另限 3 GiB 地址空间；支持的平台使用 CPU 时间和文件大小限制。
- 临时文件保存在系统临时目录下的 `pixel-local-用户ID`，随机 ID 命名。30 分钟有效，每分钟清理过期目录；停止服务时不会运行清理，下次启动补清理。再次转换会续期源图片。
- 限制本地域名与跨源 POST。此工具面向个人本机使用，没有账户系统，不应把监听地址改为公网地址。

## API

错误统一返回：

```json
{"error": {"code": "INVALID_IMAGE", "message": "中文错误说明"}}
```

`POST /api/inspect`：multipart 字段 `file`。返回 `image_id`、`filename`、`format`、`width`、`height`、`size`（字节）、`frames`、`animated`、`warnings`、`preview_url`、`expires_in`。尺寸已应用方向纠正。

`POST /api/convert`：

```json
{
  "image_id": "上传接口返回的32位ID",
  "width": 800,
  "height": 600,
  "format": "png",
  "fit": "contain",
  "background": "transparent",
  "quality": 90,
  "lossless": false
}
```

`format`：`jpeg/jpg/png/svg/gif/webp/raw/tiff/tif/ico`；`fit`：`contain/cover/stretch`；`background`：`transparent` 或 `#RRGGBB`。返回 `result_id`、结果格式/尺寸/大小、`frames`、`animated`、`warnings`、`preview_url`、`download_url`、`filename`、`expires_in`。

`GET /api/files/{id}/preview`：安全 PNG 预览；`animation`：生成的 GIF/WebP；`download`：以正确扩展名下载结果。源文件不会由文件接口暴露。

## 测试与结构

```sh
.venv/bin/python -m pytest -q
```

- `app/engine.py`：内容校验、SVG 安全处理、颜色与尺寸转换、编码。
- `app/main.py` / `app/worker.py`：接口、资源限制、子进程、临时文件生命周期。
- `app/static/`：无构建步骤的中文响应式界面。
- `tests/test_app.py`：真实格式、损坏图片、所有导出格式、动画、透明度、方向、SVG 安全、资源限制、超时与清理测试。

编码行为参考 [Pillow 文档](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)；SVG 渲染使用 [resvg-py](https://resvg-py.readthedocs.io/en/latest/api.html)。所有 Python 依赖版本锁定于 `requirements.txt`。
