"""Click-through human check carried in the published page file.

The visitor process reads that file on each request, so turning the switch
on or off reaches visitors without restarting it.
"""
import re
from pathlib import Path


SCRIPT = (
    '<script id="ab-human-click">(function(){'
    'var FLAG="ab_human",NEXT="ab-human-next",GO="ab-human-go";'
    'function read(key){try{return sessionStorage.getItem(key)||"";}catch(e){return "";}}'
    'function write(key,value){try{sessionStorage.setItem(key,value);}catch(e){}}'
    'function drop(key){try{sessionStorage.removeItem(key);}catch(e){}}'
    'function clean(){var params;try{params=new URLSearchParams(location.search);}catch(e){return location.pathname+location.hash;}'
    'params.delete(FLAG);var query=params.toString();return location.pathname+(query?"?"+query:"")+location.hash;}'
    'if(read(GO)==="1"){drop(GO);var saved=read(NEXT);drop(NEXT);var index=-1;'
    'try{index=JSON.parse(saved).index;}catch(e){}'
    'var again=document.querySelectorAll("a,button")[index];'
    'if(again){var dest=again.href||again.getAttribute("href")||"";'
    'if(again.tagName==="A"&&/^https?:\\/\\//i.test(dest))location.assign(dest);'
    'else again.click();}return;}'
    'if(new RegExp("[?&]"+FLAG+"=1(?:&|$)").test(location.search||"")){write(GO,"1");location.replace(clean());return;}'
    'document.addEventListener("click",function(event){'
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
_SCRIPT_RE = re.compile(r'<script id="ab-human-click">[\s\S]*?</script>', re.IGNORECASE)
_BODY_RE = re.compile(r'</body\s*>', re.IGNORECASE)


def stamp_published(store, enabled):
    """Add or remove the click script on the pages visitors are being shown."""
    if type(enabled) is not bool:
        raise ValueError('人机验证选项无效')
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
            if _stamp_file(path, enabled):
                changed = True
    return changed


def _stamp_file(path, enabled):
    data = path.read_bytes()
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return False
    if enabled and SCRIPT in text:
        return False
    if not enabled and 'id="ab-human-click"' not in text and "id='ab-human-click'" not in text:
        return False
    stripped = _SCRIPT_RE.sub('', text)
    updated = _insert(stripped) if enabled else stripped
    if updated == text:
        return False
    payload = updated.encode('utf-8')
    if bom:
        payload = b'\xef\xbb\xbf' + payload
    temporary = path.with_name(path.name + '.human.tmp')
    temporary.write_bytes(payload)
    temporary.replace(path)
    return True


def _insert(text):
    match = None
    for found in _BODY_RE.finditer(text):
        match = found
    if match is None:
        return text + SCRIPT
    return text[:match.start()] + SCRIPT + text[match.start():]
