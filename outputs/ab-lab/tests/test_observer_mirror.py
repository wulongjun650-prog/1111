import hashlib

from ablab.archives import write_bundle
from ablab.mirror import sync_mirror_content
from ablab.redirects import read_direct_mode
from ablab.sites import Registry


def publish(store, slot, html):
    version = write_bundle([('index.html', html)], 'page.html', store.pages, hashlib.sha256(html).hexdigest())
    store.add_version(slot, version)
    store.publish(slot, version['id'])
    return version


def test_mirror_keeps_the_questionnaire_and_does_not_copy_visits(tmp_path):
    registry = Registry(tmp_path)
    source_site = registry.add('source.test')
    mirror_site = registry.add('watch.test')
    source = registry.store(source_site['id'])
    mirror = registry.store(mirror_site['id'])
    html = b"<html><script>directMode: 'ABCABC'</script><p>85290001111</p></html>"
    publish(source, 'B', html)
    source.add_whatsapp_numbers(['85290001111'], '值班', 'Vivian')
    config, revision = source.config()
    source.save_config(config.model_copy(update={'allowed_slot': 'A'}), revision)
    with source.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                   (1, '203.0.113.0/24', 'KR', 'mobile', 'B', 'allowed', '/', 'RULES'))
    source.record_client_event('{"event":"page_view","page":"https://source.test/"}', 'source.test')

    assert sync_mirror_content(source, mirror) is True
    copied = (mirror.pages / mirror.slots()['B'] / 'index.html').read_text()
    assert "directMode: ''" in copied
    assert 'ABCABC' not in copied and "directMode: 'ABC'" not in copied
    assert read_direct_mode(copied) == 'CBA'
    assert read_direct_mode((source.pages / source.slots()['B'] / 'index.html').read_text()) == 'ABCABC'
    assert [item['phone'] for item in mirror.whatsapp_numbers()] == ['85290001111']
    assert mirror.config()[0].allowed_slot == 'A'
    assert mirror.logs(30, '', 1)['total'] == 0
    with mirror.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM client_events').fetchone()[0] == 0
    with source.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    versions = mirror.slots()['B']
    assert sync_mirror_content(source, mirror) is False
    assert mirror.slots()['B'] == versions


def test_mirror_keeps_the_contact_flow_questionnaire(tmp_path):
    registry = Registry(tmp_path)
    source_site = registry.add('source.test')
    mirror_site = registry.add('watch.test')
    source = registry.store(source_site['id'])
    mirror = registry.store(mirror_site['id'])
    html = """<html><script>
function getContactFlowMode(){
  const params=new URLSearchParams(location.search);
  const code=String(params.get('code')||params.get('mode')||'CBA').trim().toUpperCase();
  if(code==='CBA')return'CBA';
  return'ABC';
}
const contactFlowMode=getContactFlowMode();
const useContactGate=contactFlowMode==='ABC';
</script></html>""".encode()
    publish(source, 'B', html)
    assert sync_mirror_content(source, mirror) is True
    copied = (mirror.pages / mirror.slots()['B'] / 'index.html').read_text()
    assert "||'ABC'" in copied
    assert "||'CBA'" not in copied
    assert read_direct_mode(copied) == 'CBA'
    assert read_direct_mode((source.pages / source.slots()['B'] / 'index.html').read_text()) == 'ABC'


def test_binding_rejects_a_mirror_used_as_the_source(tmp_path):
    registry = Registry(tmp_path)
    source = registry.add('source.test')
    watch = registry.add('watch.test')
    other = registry.add('other.test')
    observer = 'a' * 32
    registry.bind_mirror(watch['id'], source['id'], observer)
    try:
        registry.bind_mirror(other['id'], watch['id'], 'b' * 32)
    except ValueError as error:
        assert '不能是另一个观察域名' in str(error)
    else:
        raise AssertionError('a watch domain was accepted as a source')
    moved = registry.bind_mirror(other['id'], source['id'], observer)
    assert moved['mirror_source'] == source['id']
    assert registry.get(watch['id'])['observer_account'] == ''
    assert registry.get(watch['id'])['mirror_source'] == ''
