import pytest

from ablab.redirects import read_direct_mode, set_direct_mode

CONTACT_FLOW_PAGE = '''<!doctype html>
<dialog id="contact-gate" class="contact-gate"></dialog>
<script>
const SETTINGS = {
  TARGET_URLS: ['https://api.whatsapp.com/send?phone=85290000000'],
  MESSAGE_TEMPLATE: '你好',
  TRACKING_WAIT_MS: 180
};
function getContactFlowMode(){
  const params=new URLSearchParams(location.search);
  const code=String(params.get('code')||params.get('mode')||'ABC').trim().toUpperCase();
  if(code==='CBA')return'CBA';
  return'ABC';
}
const contactFlowMode=getContactFlowMode();
const useContactGate=contactFlowMode==='ABC';
function startContact(button,recordCta=true){
  if(!useContactGate){return;}
}
function handleContactButton(button){
  if(!useContactGate){return;}
}
</script>
'''


def test_contact_flow_upload_reads_cba_when_page_default_is_abc():
    assert read_direct_mode(CONTACT_FLOW_PAGE) == 'CBA'


def test_contact_flow_secret_weapon_toggles_without_touching_handlers():
    direct = set_direct_mode(CONTACT_FLOW_PAGE, 'ABC')
    assert 'CONTACT_FLOW_DEFAULT: \'CBA\'' in direct
    assert 'SETTINGS.CONTACT_FLOW_DEFAULT||\'CBA\'' in direct
    assert read_direct_mode(direct) == 'ABC'
    restored = set_direct_mode(direct, 'CBA')
    assert read_direct_mode(restored) == 'CBA'
    assert 'CONTACT_FLOW_DEFAULT: \'ABC\'' in restored


def test_contact_flow_refuses_unknown_direct_mode():
    with pytest.raises(ValueError):
        set_direct_mode(CONTACT_FLOW_PAGE, 'XYZ')
