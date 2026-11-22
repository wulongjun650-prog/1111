"""Human-check snippets carried in the published page file.

The visitor process reads that file on each request, so turning a switch
on or off reaches visitors without restarting it.
"""
import re

from . import visitor_surface as surface

_FLAG = surface.HUMAN_BUTTON_QUERY_FLAG
_LEGACY_FLAG = surface.LEGACY_HUMAN_BUTTON_QUERY_FLAG
_CLICK_ID = surface.HUMAN_CLICK_SCRIPT_ID
_LEGACY_CLICK_ID = surface.LEGACY_HUMAN_CLICK_SCRIPT_ID
_STYLE_ID = surface.HUMAN_BROWSE_STYLE_ID
_SLOT_ID = surface.HUMAN_BROWSE_DIV_ID
_LOADER_ID = surface.HUMAN_BROWSE_LOADER_ID
_JS_NAME = surface.HUMAN_BROWSE_JS_NAME
_JS_MARK = surface.HUMAN_BROWSE_JS_MARK
_LEGACY_STYLE_ID = surface.LEGACY_HUMAN_BROWSE_STYLE_ID
_LEGACY_SLOT_ID = surface.LEGACY_HUMAN_BROWSE_DIV_ID
_LEGACY_LOADER_ID = surface.LEGACY_HUMAN_BROWSE_LOADER_ID
_LEGACY_JS_NAME = surface.LEGACY_HUMAN_BROWSE_JS_NAME
_LEGACY_JS_MARK = surface.LEGACY_HUMAN_BROWSE_JS_MARK
_WAIT_ID = 'pg-verify-wait'
_PENDING = 'pgVerifyPending'
_SESSION_NEXT = 'pg-verify-next'
_SESSION_GO = 'pg-verify-go'

SCRIPT = (
    f'<script id="{_CLICK_ID}">(function(){{'
    f'var FLAG="{_FLAG}",LEGACY="{_LEGACY_FLAG}",NEXT="{_SESSION_NEXT}",GO="{_SESSION_GO}";'
    'function read(key){try{return sessionStorage.getItem(key)||"";}catch(e){return "";}}'
    'function write(key,value){try{sessionStorage.setItem(key,value);}catch(e){}}'
    'function drop(key){try{sessionStorage.removeItem(key);}catch(e){}}'
    'function clean(){var params;try{params=new URLSearchParams(location.search);}catch(e){return location.pathname+location.hash;}'
    'params.delete(FLAG);params.delete(LEGACY);var query=params.toString();return location.pathname+(query?"?"+query:"")+location.hash;}'
    'function flagged(){return [FLAG,LEGACY].some(function(name){return new RegExp("[?&]"+name+"=1(?:&|$)").test(location.search||"");});}'
    'if(read(GO)==="1"){drop(GO);var saved=read(NEXT);drop(NEXT);var index=-1;'
    'try{index=JSON.parse(saved).index;}catch(e){}'
    'var again=document.querySelectorAll("a,button")[index];'
    'if(again){var dest=again.href||again.getAttribute("href")||"";'
    'if(again.tagName==="A"&&/^https?:\\/\\//i.test(dest))location.assign(dest);'
    'else again.click();}return;}'
    'if(flagged()){write(GO,"1");location.replace(clean());return;}'
    'document.addEventListener("click",function(event){'
    f'if(window.{_PENDING})return;'
    'var node=event.target&&event.target.closest?event.target.closest("a,button"):null;'
    'if(!node)return;'
    'var href=node.href||node.getAttribute("href")||"";'
    'if(node.tagName==="A"&&(!href||href.charAt(0)==="#"||href.indexOf("javascript:")===0))return;'
    'event.preventDefault();event.stopPropagation();'
    'if(event.stopImmediatePropagation)event.stopImmediatePropagation();'
    'var nodes=document.querySelectorAll("a,button");'
    'var index=Array.prototype.indexOf.call(nodes,node);'
    'write(NEXT,JSON.stringify({index:index}));'
    'var params;try{params=new URLSearchParams(location.search);}catch(e){params=new URLSearchParams();}'
    'params.set(FLAG,"1");'
    'location.assign(location.pathname+"?"+params.toString()+location.hash);'
    '},true);'
    '})();</script>'
)

_SCRIPT_RES = (
    rf'<script id="{re.escape(_CLICK_ID)}">[\s\S]*?</script>',
    rf'<script id="{re.escape(_LEGACY_CLICK_ID)}">[\s\S]*?</script>',
)
_BROWSE_RES = (
    rf'<style id="{re.escape(_STYLE_ID)}">[\s\S]*?</style>'
    rf'|<div id="{re.escape(_SLOT_ID)}"></div>'
    rf'|<script id="{re.escape(_LOADER_ID)}"(?:\s[^>]*)?>[\s\S]*?</script>',
    rf'<style id="{re.escape(_LEGACY_STYLE_ID)}">[\s\S]*?</style>'
    rf'|<div id="{re.escape(_LEGACY_SLOT_ID)}"></div>'
    rf'|<script id="{re.escape(_LEGACY_LOADER_ID)}"(?:\s[^>]*)?>[\s\S]*?</script>',
)
_BODY_RE = re.compile(r'</body\s*>', re.IGNORECASE)
_SITEKEY_RE = re.compile(r'^0x[A-Za-z0-9_-]{20,80}$')


def _strip_stamped(text):
    updated = text
    for pattern in _SCRIPT_RES:
        updated = re.sub(pattern, '', updated, flags=re.IGNORECASE)
    for pattern in _BROWSE_RES:
        updated = re.sub(pattern, '', updated, flags=re.IGNORECASE)
    return updated


def browse_script(sitekey, wait=False):
    """Widget at the end of the page. The script file is deferred, so it does not hold up the page."""
    if not isinstance(sitekey, str) or not _SITEKEY_RE.fullmatch(sitekey) or type(wait) is not bool:
        raise ValueError('人机验证选项无效')
    notice = (
        f'#{_WAIT_ID}{{position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483000;'
        'max-width:calc(100% - 32px);padding:10px 14px;border-radius:10px;background:#17233a;color:#fff;'
        'font:14px/1.4 -apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei",sans-serif;'
        'text-align:center;pointer-events:none}'
    ) if wait else ''
    return (
        f'<style id="{_STYLE_ID}">#{_SLOT_ID}{{display:flex;justify-content:center;'
        'width:100%;clear:both;margin:28px 0 20px;padding:0 12px}'
        f'script#{_LOADER_ID},style#{_STYLE_ID}{{display:none!important}}'
        f'{notice}</style>'
        f'<div id="{_SLOT_ID}"></div>'
        f'<script id="{_LOADER_ID}" src="{_JS_NAME}" defer></script>'
    )


def browse_javascript(sitekey, wait=False):
    """The check itself lives beside the page, so its source is not drawn as text."""
    if not isinstance(sitekey, str) or not _SITEKEY_RE.fullmatch(sitekey) or type(wait) is not bool:
        raise ValueError('人机验证选项无效')
    gate = ''
    callback = 'function(){}'
    if wait:
        gate = (
            f'var ready=false;window.{_PENDING}=true;'
            'document.addEventListener("click",function(event){'
            'if(ready)return;'
            'var node=event.target&&event.target.closest?event.target.closest("a,button"):null;'
            'if(!node)return;'
            'var href=node.href||node.getAttribute("href")||"";'
            'if(node.tagName==="A"&&(!href||href.charAt(0)==="#"||href.indexOf("javascript:")===0))return;'
            'event.preventDefault();event.stopPropagation();'
            'if(event.stopImmediatePropagation)event.stopImmediatePropagation();'
            f'var note=document.getElementById("{_WAIT_ID}");'
            f'if(!note){{note=document.createElement("div");note.id="{_WAIT_ID}";'
            'note.textContent="请稍等，正在验证是否是真人";document.body.appendChild(note);}'
            '},true);'
        )
        callback = (
            f'function(){{ready=true;window.{_PENDING}=false;'
            f'var note=document.getElementById("{_WAIT_ID}");if(note)note.remove();}}'
        )
    return (
        '(function(){'
        f'var KEY="{sitekey}";'
        f'{gate}'
        f'var box=document.getElementById("{_SLOT_ID}");'
        f'if(!box){{box=document.createElement("div");box.id="{_SLOT_ID}";'
        '(document.body||document.documentElement).appendChild(box);}'
        'function show(){if(!window.turnstile||box.getAttribute("data-ready"))return;'
        'box.setAttribute("data-ready","1");'
        'window.turnstile.render(box,{sitekey:KEY,theme:"light",size:"normal",appearance:"always",callback:'
        f'{callback}'
        '});}'
        'if(window.turnstile){show();return;}'
        'var node=document.createElement("script");'
        'node.src="https://challenges.cloudflare.com/turnstile/v0/api.js";'
        'node.async=true;node.onload=show;document.head.appendChild(node);'
        '})();\n'
    )


def stamp_published(store, button=False, browse=False, sitekey='', wait=False):
    """Add or remove the human-check snippets on the pages visitors are being shown."""
    if type(button) is not bool or type(browse) is not bool or type(wait) is not bool:
        raise ValueError('人机验证选项无效')
    widget = browse or wait
    if widget and (not isinstance(sitekey, str) or not _SITEKEY_RE.fullmatch(sitekey)):
        raise ValueError('人机验证选项无效')
    piece = browse_script(sitekey, wait) if widget else ''
    script = browse_javascript(sitekey, wait) if widget else ''
    changed = False
    pages = store.pages.resolve()
    seen = set()
    for version_id in store.slots().values():
        if not isinstance(version_id, str) or version_id in seen or not re.fullmatch(r'[a-f0-9]{32}', version_id):
            continue
        seen.add(version_id)
        root = (pages / version_id).resolve()
        if root.parent != pages or not root.is_dir():
            continue
        for path in root.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in ('.html', '.htm'):
                continue
            if path.stat().st_size > 3_000_000:
                continue
            if _stamp_file(path, button, piece, script):
                changed = True
    return changed


def _stamp_file(path, button, browse_html, browse_js):
    data = path.read_bytes()
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return _sync_script(path, browse_js)
    stripped = _strip_stamped(text)
    stripped = surface.polish_visitor_text(stripped, path.name)
    updated = stripped
    if browse_html:
        updated = _insert(updated, browse_html)
    if button:
        updated = _insert(updated, SCRIPT)
    changed = _sync_script(path, browse_js)
    if updated == text:
        return changed
    payload = updated.encode('utf-8')
    if bom:
        payload = b'\xef\xbb\xbf' + payload
    temporary = path.with_name(path.name + '.human.tmp')
    temporary.write_bytes(payload)
    temporary.replace(path)
    return True


def _sync_script(path, browse_js):
    primary = path.with_name(_JS_NAME)
    legacy = path.with_name(_LEGACY_JS_NAME)
    if primary.parent != path.parent:
        return False
    changed = False
    if browse_js:
        payload = browse_js.encode('utf-8')
        if not (primary.is_file() and primary.read_bytes() == payload):
            if primary.is_file():
                current = primary.read_bytes()
                if not (current.startswith(_JS_MARK) or current.startswith(_LEGACY_JS_MARK)):
                    return False
            temporary = primary.with_name(primary.name + '.human.tmp')
            temporary.write_bytes(payload)
            temporary.replace(primary)
            changed = True
        if legacy.is_file() and legacy.read_bytes().startswith(_LEGACY_JS_MARK):
            legacy.unlink()
            changed = True
        return changed
    for script, mark in ((primary, _JS_MARK), (legacy, _LEGACY_JS_MARK)):
        if script.is_file() and script.read_bytes().startswith(mark):
            script.unlink()
            changed = True
    return changed


def _insert(text, piece):
    match = None
    for found in _BODY_RE.finditer(text):
        match = found
    if match is None:
        return text + piece
    return text[:match.start()] + piece + text[match.start():]
