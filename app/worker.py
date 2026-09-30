"""One job per process; the parent enforces the wall-clock deadline."""
import json
from pathlib import Path
import sys

from app.engine import ImageError, convert_image, inspect_image


def main():
    job_path = Path(sys.argv[1])
    result_path = job_path.parent / 'worker-result.json'
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
            # macOS does not reliably support RLIMIT_AS. Pixel/frame limits apply everywhere.
            if sys.platform.startswith('linux'):
                resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        except (ImportError, ValueError, OSError):
            pass
        job = json.loads(job_path.read_text())
        if job['action'] == 'inspect':
            data = inspect_image(job['source'], job['filename'], str(job_path.parent))
        else:
            data = convert_image(job['source'], job['options'], str(job_path.parent))
        result = {'ok': True, 'data': data}
    except ImageError as exc:
        result = {'ok': False, 'code': exc.code, 'message': exc.message}
    except Exception:
        result = {'ok': False, 'code': 'PROCESSING_FAILED', 'message': '图片处理失败：文件损坏、编码不受支持或资源不足。'}
    result_path.write_text(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
