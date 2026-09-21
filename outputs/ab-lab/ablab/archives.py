"""Bounded, all-or-nothing import. Uploaded files are NEVER templates."""
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata
import uuid
import zipfile

MAX_ZIP = 20 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_FILES = 500
EXTENSIONS = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.txt', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.mp4', '.webm', '.mp3', '.wav'}
SCRIPT_PARTS = {'.php', '.phtml', '.php3', '.php4', '.php5', '.py', '.sh', '.exe', '.dll', '.asp', '.aspx', '.cgi', '.pl', '.shtml', '.bat', '.cmd', '.ps1'}
TEXT_EXTENSIONS = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.txt', '.svg'}


def safe_parts(name):
    if not name or len(name) > 240 or name.startswith('/') or '\\' in name or '%' in name or any(ord(c) < 32 for c in name):
        raise ValueError('文件路径不安全')
    parts = name.split('/')
    for part in parts:
        if not part or part in ('.', '..') or part.startswith('.') or part.endswith((' ', '.')) or any(c in part for c in ':<>"|?*'):
            raise ValueError('文件路径不安全')
        if re.fullmatch(r'(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', part, re.I):
            raise ValueError('不支持系统保留文件名')
    return parts


def validate_file(name, data):
    suffix = Path(name).suffix.lower()
    if suffix not in EXTENSIONS or any(x.lower() in SCRIPT_PARTS for x in Path(name).suffixes):
        raise ValueError('仅允许静态网页资源，不允许服务器脚本或配置')
    if len(data) > MAX_FILE:
        raise ValueError('单文件超过 20 MiB')
    if data.startswith((b'MZ', b'\x7fELF', b'#!')):
        raise ValueError('文件内容不是允许的静态资源')
    if suffix in TEXT_EXTENSIONS:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('文本资源须为 UTF-8 编码') from exc
        if '\x00' in text or re.search(r'<\?(?:php|=)|<%', text, re.I):
            raise ValueError('检测到服务器脚本标记或二进制文本')


def import_content(data: bytes, filename: str, pages: Path):
    if not data or len(data) > MAX_ZIP:
        raise ValueError('文件为空或超过 20 MiB')
    pages.mkdir(parents=True, exist_ok=True)
    files = []
    if Path(filename).suffix.lower() in ('.html', '.htm'):
        validate_file('index.html', data)
        files.append(('index.html', data))
    elif Path(filename).suffix.lower() == '.zip':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES * 2:
                    raise ValueError('ZIP 条目过多')
                seen, total = set(), 0
                for info in infos:
                    name = info.orig_filename.rstrip('/')
                    safe_parts(name)
                    canonical = unicodedata.normalize('NFC', name).casefold()
                    if canonical in seen:
                        raise ValueError('ZIP 含重复或大小写冲突的路径')
                    seen.add(canonical)
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                        raise ValueError('不允许链接、特殊文件或加密 ZIP')
                    if info.is_dir():
                        continue
                    if file_type == stat.S_IFDIR or info.file_size > MAX_FILE:
                        raise ValueError('文件类型或单文件大小不符合限制')
                    total += info.file_size
                    if total > MAX_TOTAL or len(files) >= MAX_FILES or info.file_size > max(info.compress_size, 1) * 1000:
                        raise ValueError('ZIP 超过解压限制或压缩比异常')
                    with archive.open(info) as member:
                        payload = member.read(MAX_FILE + 1)
                    if len(payload) != info.file_size:
                        raise ValueError('ZIP 成员大小异常')
                    validate_file(name, payload)
                    files.append((name, payload))
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as exc:
            raise ValueError('ZIP 损坏或压缩格式不支持') from exc
    else:
        raise ValueError('请上传 ZIP 或单个 HTML 文件')
    names = [name for name, _ in files]
    if 'index.html' not in names:
        wrappers = {name.split('/')[0] for name in names}
        if len(wrappers) == 1 and all('/' in name for name in names):
            files = [(name.split('/', 1)[1], payload) for name, payload in files]
    if 'index.html' not in [name for name, _ in files]:
        raise ValueError('需要根目录 index.html（可有一层外包装目录）')
    canonical_files = {unicodedata.normalize('NFC', name).casefold() for name, _ in files}
    for name, _ in files:
        parts = safe_parts(name)
        if any('/'.join(parts[:i]).casefold() in canonical_files for i in range(1, len(parts))):
            raise ValueError('文件与目录路径冲突')
    version_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=pages) as staging:
        content = Path(staging) / 'content'
        content.mkdir()
        for name, payload in files:
            target = content.joinpath(*safe_parts(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        os.replace(content, pages / version_id)
    return {'id': version_id, 'name': Path(filename.replace('\\', '/')).name[:160], 'created': time.time(), 'files': len(files), 'bytes': sum(len(payload) for _, payload in files), 'sha256': hashlib.sha256(data).hexdigest()}


def resolve_file(root: Path, path: str):
    path = path or 'index.html'
    if path.endswith('/'):
        path += 'index.html'
    candidate = root.joinpath(*safe_parts(path)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError('越界路径')
    if candidate.is_dir():
        candidate /= 'index.html'
    if not candidate.is_file():
        raise FileNotFoundError(path)
    return candidate
