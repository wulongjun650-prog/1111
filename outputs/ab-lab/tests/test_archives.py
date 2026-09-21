import io
import stat
import zipfile
import pytest
from ablab.archives import import_content, resolve_file


def zipped(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, data in files:
            if isinstance(name, str):
                info = zipfile.ZipInfo(name)
                # Preserve hostile raw names even on Windows (ZipInfo normalizes os.sep).
                info.filename = name
                info.orig_filename = name
            else:
                info = name
            z.writestr(info, data)
    return out.getvalue()


def test_import_unwraps_one_directory_and_preserves_resources(tmp_path):
    result = import_content(zipped([('site/index.html', '<h1>A</h1>'), ('site/css/main.css', 'body{}')]), 'a.zip', tmp_path)
    root = tmp_path / result['id']
    assert (root / 'index.html').read_text() == '<h1>A</h1>'
    assert (root / 'css/main.css').is_file()
    assert result['files'] == 2
    assert len(result['sha256']) == 64


@pytest.mark.parametrize('name', ['../x.html', '/x.html', 'C:/x.html', 'a/../../x.html', 'a\\x.html', '.env', 'x.php', 'x.php.html', 'web.config', 'a:stream.html', 'CON.html', 'foo./index.html'])
def test_bad_member_rejects_entire_upload_without_publication(tmp_path, name):
    with pytest.raises(ValueError):
        import_content(zipped([('index.html', '<h1>ok</h1>'), (name, 'bad')]), 'a.zip', tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_symlink_rejected(tmp_path):
    symlink = zipfile.ZipInfo('link.html')
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        import_content(zipped([('index.html', 'ok'), (symlink, '/etc/passwd')]), 'a.zip', tmp_path)


def test_case_collisions_and_no_index_rejected(tmp_path):
    for files in [[('index.html', 'ok'), ('INDEX.html', 'other')], [('page.html', 'no index')]]:
        with pytest.raises(ValueError):
            import_content(zipped(files), 'a.zip', tmp_path)


def test_raw_html_and_executable_disguise(tmp_path):
    result = import_content(b'<!doctype html><h1>single</h1>', 'single.html', tmp_path)
    assert resolve_file(tmp_path / result['id'], '').name == 'index.html'
    with pytest.raises(ValueError):
        import_content(b'MZ' + b'\x00' * 20, 'evil.html', tmp_path)


@pytest.mark.parametrize('path', ['../secret', '%2e%2e/x', 'a\\b', '/secret', 'a:stream'])
def test_request_path_stays_inside_version(tmp_path, path):
    with pytest.raises(ValueError):
        resolve_file(tmp_path, path)


def test_compressed_bomb_rejected(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('index.html', 'a' * (21 * 1024 * 1024))
    with pytest.raises(ValueError):
        import_content(out.getvalue(), 'bomb.zip', tmp_path)
