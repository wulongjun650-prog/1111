"""Human-check snippets carried in the published page file.

The visitor process reads that file on each request, so turning a switch
on or off reaches visitors without restarting it.
"""
import re


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
_BROWSE_RE = re.compile(
    r'<style id="ab-human-browse-style">[\s\S]*?</style>|<div id="ab-human-browse"></div>|<script id="ab-human-browse">[\s\S]*?</script>',
    re.IGNORECASE,
)
_BODY_RE = re.compile(r'</body\s*>', re.IGNORECASE)
_SITEKEY_RE = re.compile(r'^0x[A-Za-z0-9_-]{20,80}$')


def browse_script(sitekey):
    """Corner widget. The page stays usable while Cloudflare draws the checkbox."""
    if not isinstance(sitekey, str) or not _SITEKEY_RE.fullmatch(sitekey):
        raise ValueError('人机验证选项无效')
    return (
        '<style id="ab-human-browse-style">#ab-human-browse{position:fixed;right:12px;bottom:12px;'
        'z-index:2147483000;width:max-content;max-width:calc(100vw - 24px)}</style>'
        '<div id="ab-human-browse"></div>'
        '<script id="ab-human-browse">(function(){'
        f'var KEY="{sitekey}";'
        'var box=document.getElementById("ab-human-browse");'
        'if(!box){box=document.createElement("div");box.id="ab-human-browse";'
        '(document.body||document.documentElement).appendChild(box);}'
        'function show(){if(!window.turnstile||box.getAttribute("data-ready"))return;'
        'box.setAttribute("data-ready","1");'
        'window.turnstile.render(box,{sitekey:KEY,theme:"light",size:"normal",appearance:"always",callback:function(){}});}'
        'if(window.turnstile){show();return;}'
        'var node=document.createElement("script");'
        'node.src="https://challenges.cloudflare.com/turnstile/v0/api.js";'
        'node.async=true;node.onload=show;document.head.appendChild(node);'
        '})();</script>'
    )


def stamp_published(store, button=False, browse=False, sitekey=''):
    """Add or remove the human-check snippets on the pages visitors are being shown."""
    if type(button) is not bool or type(browse) is not bool:
        raise ValueError('人机验证选项无效')
    if browse and (not isinstance(sitekey, str) or not _SITEKEY_RE.fullmatch(sitekey)):
        raise ValueError('人机验证选项无效')
    piece = browse_script(sitekey) if browse else ''
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
            if _stamp_file(path, button, piece):
                changed = True
    return changed


def _stamp_file(path, button, browse_html):
    data = path.read_bytes()
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return False
    stripped = _BROWSE_RE.sub('', _SCRIPT_RE.sub('', text))
    updated = stripped
    if browse_html:
        updated = _insert(updated, browse_html)
    if button:
        updated = _insert(updated, SCRIPT)
    if updated == text:
        return False
    payload = updated.encode('utf-8')
    if bom:
        payload = b'\xef\xbb\xbf' + payload
    temporary = path.with_name(path.name + '.human.tmp')
    temporary.write_bytes(payload)
    temporary.replace(path)
    return True


def _insert(text, piece):
    match = None
    for found in _BODY_RE.finditer(text):
        match = found
    if match is None:
        return text + piece
    return text[:match.start()] + piece + text[match.start():]
