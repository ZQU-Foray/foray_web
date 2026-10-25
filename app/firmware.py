"""固件文件管理：列表 / 上传 / 删除。"""
import tempfile
from pathlib import Path

from . import config


def _safe_path(name: str):
    """防目录穿越：限定在固件目录内。"""
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(c in name for c in '/\\{}')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or Path(name).is_absolute()):
        raise ValueError("非法路径")
    root = config.FIRMWARE_DIR.resolve()
    p = root / name
    if p.is_symlink() or p.resolve().parent != root:
        raise ValueError("非法路径")
    return p


def list_firmware():
    items = []
    for p in sorted(config.FIRMWARE_DIR.iterdir()):
        if not p.is_symlink() and p.is_file() and p.suffix.lower() in config.ALLOWED_EXTS:
            st = p.stat()
            items.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return items


def save_upload(filename: str, data: bytes):
    from io import BytesIO
    return save_upload_stream(filename, BytesIO(data))


def save_upload_stream(filename, source):
    """限量分块复制到临时文件，成功后原子替换；失败保留旧固件。"""
    p = _safe_path(filename)
    if p.suffix.lower() not in config.ALLOWED_EXTS:
        raise ValueError("仅支持 ELF、HEX、BIN 固件")
    size = 0
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=p.parent, prefix=".upload-", delete=False) as dst:
            tmp = Path(dst.name)
            while chunk := source.read(min(1024 * 1024, config.MAX_UPLOAD_BYTES - size + 1)):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise ValueError("文件超过 64MB 上限")
                dst.write(chunk)
        tmp.replace(p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"name": p.name, "size": p.stat().st_size}


def delete_firmware(name: str):
    p = _safe_path(name)
    p.unlink(missing_ok=True)
