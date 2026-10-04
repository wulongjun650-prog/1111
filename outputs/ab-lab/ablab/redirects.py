"""Static navigation spans only. Never execute source or replace arbitrary URLs."""
import bisect
from dataclasses import dataclass
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .archives import MAX_TOTAL, read_source, source_files, validate_file, write_bundle

MAX_OCCURRENCES = 5000
MAX_SCAN_TOKENS = 100000
MAX_EXPRESSION_TOKENS = 128
NAVIGATION = {'location', 'window.location', 'document.location', 'top.location', 'self.location', 'parent.location'}
DOM_LOOKUPS = {'document.querySelector', 'document.getElementById', 'document.createElement'}
ATTR = re.compile(r'''(?<!\S)([^\s=<>/'"]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''')
ENTITY = re.compile(r'&(?:#[xX][0-9a-fA-F]+;?|#[0-9]+;?|[a-zA-Z][a-zA-Z0-9]+;)')
LEX = re.compile(r'''\s+|//[^\n\r]*|/\*[\s\S]*?\*/|(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`)|[\w$]+|===|!==|==|!=|=>|\+=|&&|\|\||\?\?|[^\s]''')


@dataclass
class Token:
    value: str
    start: int
    end: int
    kind: str = ''


def _string(raw):
    if raw[0] == '`' and '${' in raw:
        return None
    def escape(match):
        text = match.group(1)
        if text.startswith(('u', 'x')) and len(text) > 1:
            try:
                return chr(int(text[1:], 16))
            except ValueError:
                return '\\' + text
        return {'n':'\n', 'r':'\r', 't':'\t', 'b':'\b', 'f':'\f', 'v':'\v', '0':'\0', '\n':'', '\r\n':''}.get(text, text)
    return re.sub(r'\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])', escape, raw[1:-1])


def _tokens(text):
    tokens, offset, parens, control_closed = [], 0, [], False
    while offset < len(text):
        if len(tokens) > MAX_SCAN_TOKENS:
            raise ValueError('源码过于复杂，超过静态扫描词元限制；请使用源码编辑器核对')
        match = LEX.match(text, offset)
        if not match:
            offset += 1
            continue
        raw, start = match.group(), offset
        offset = match.end()
        if raw.isspace() or raw.startswith(('//', '/*')):
            continue
        # A regex literal is data, even if its pattern spells location.href.
        if raw == '/' and (control_closed or not tokens or tokens[-1].value in ('=', '(', ',', ':', '[', '!', 'return', '=>', '?', ';', '{', '}')):
            index, bracket = offset, False
            while index < len(text) and text[index] not in '\r\n':
                char = text[index]
                if char == '\\':
                    index += 2
                    continue
                if char == '[':
                    bracket = True
                elif char == ']':
                    bracket = False
                elif char == '/' and not bracket:
                    index += 1
                    while index < len(text) and text[index].isalpha():
                        index += 1
                    offset = index
                    break
                index += 1
            tokens.append(Token('<regex>', start, offset, 'other'))
            control_closed = False
            continue
        if raw == '(':
            parens.append(tokens[-1].value if tokens else '')
        control_closed = raw == ')' and bool(parens) and parens.pop() in ('if', 'while', 'for', 'with', 'switch', 'catch')
        kind = 'string' if raw[0] in "\"'`" else 'identifier' if re.fullmatch(r'[\w$]+', raw) else 'punct'
        tokens.append(Token(raw, start, offset, kind))
        if len(tokens) > MAX_SCAN_TOKENS:
            raise ValueError('源码过于复杂，超过静态扫描词元限制；请使用源码编辑器核对')
    return tokens


def _member(tokens, start):
    if tokens[start].kind != 'identifier':
        return '', start
    parts, index = [tokens[start].value], start + 1
    while index < len(tokens):
        if index - start >= MAX_EXPRESSION_TOKENS:
            raise ValueError('源码表达式过于复杂；请使用源码编辑器核对')
        if index + 1 < len(tokens) and tokens[index].value == '.' and tokens[index + 1].kind == 'identifier':
            parts.append(tokens[index + 1].value)
            index += 2
        elif index + 2 < len(tokens) and tokens[index].value == '[' and tokens[index + 1].kind == 'string' and tokens[index + 2].value == ']':
            value = _string(tokens[index + 1].value)
            if value is None:
                break
            parts.append(value)
            index += 3
        elif tokens[index].value == '(' and '.'.join(parts) in DOM_LOOKUPS:
            finish = _expression_end(tokens, index + 1, '')
            if finish >= len(tokens) or tokens[finish].value != ')':
                break
            parts[-1] += '()'
            index = finish + 1
        else:
            break
    return '.'.join(parts), index


def _expression_end(tokens, start, text):
    depth, index = 0, start
    while index < len(tokens):
        if index - start >= MAX_EXPRESSION_TOKENS:
            raise ValueError('源码表达式过于复杂；请使用源码编辑器核对')
        value = tokens[index].value
        if not depth and (value in (';', ',', ')', '}', ']') or (index > start and '\n' in text[tokens[index-1].end:tokens[index].start] and value not in ('+', '?', ':') and tokens[index-1].value not in ('+', '?', ':'))):
            break
        if value in ('(', '[', '{'):
            depth += 1
        elif value in (')', ']', '}'):
            depth -= 1
        index += 1
    return index


def _unparen(tokens):
    while len(tokens) >= 2 and tokens[0].value == '(' and tokens[-1].value == ')':
        depth, wrapped = 0, True
        for index, token in enumerate(tokens):
            depth += (token.value == '(') - (token.value == ')')
            if not depth and index < len(tokens)-1:
                wrapped = False
                break
        if not wrapped:
            break
        tokens = tokens[1:-1]
    return tokens


def _resolve(tokens, constants, seen=()):
    tokens = _unparen(tokens)
    if len(tokens) == 1:
        token = tokens[0]
        if token.kind == 'string':
            return _string(token.value)
        if token.kind == 'identifier' and constants.get(token.value) and token.value not in seen and len(seen) < 16:
            return _resolve(constants[token.value], constants, (*seen, token.value))
        return None
    if tokens and len(tokens) % 2 and all(tokens[index].value == '+' for index in range(1, len(tokens), 2)):
        values = [_resolve([tokens[index]], constants, seen) for index in range(0, len(tokens), 2)]
        if all(value is not None for value in values) and sum(map(len, values)) <= 4096:
            return ''.join(values)
    return None


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value.startswith('#') or any(ord(c) < 32 for c in value):
        return False
    try:
        scheme = urlsplit(value).scheme.lower()
        return scheme in ('http', 'https', 'whatsapp', 'bandapp') or not scheme
    except ValueError:
        return False


def _decoded(raw):
    """Entity decoding with offsets back to the original attribute source."""
    text, starts, ends, cursor = [], [], [], 0
    for match in ENTITY.finditer(raw):
        for index in range(cursor, match.start()):
            text.append(raw[index]); starts.append(index); ends.append(index + 1)
        for char in html.unescape(match.group()):
            text.append(char); starts.append(match.start()); ends.append(match.end())
        cursor = match.end()
    for index in range(cursor, len(raw)):
        text.append(raw[index]); starts.append(index); ends.append(index + 1)
    return ''.join(text), starts, ends


class Scanner:
    def __init__(self, path, text):
        self.path, self.text, self.spans, self.warnings, self.positions = path, text, [], [], set()
        self.token_count = 0
        self.lines = [0] + [match.end() for match in re.finditer('\n', text)]

    def add(self, start, end, url, kind, mode='js', prefix='', suffix='', quote=True):
        if not _url(url):
            return
        if len(self.spans) >= MAX_OCCURRENCES:
            raise ValueError('跳转位置超过 5000 处，请缩小源码包后重试')
        if (start, end) in self.positions:
            return
        self.positions.add((start, end))
        self.spans.append({'path':self.path, 'line':bisect.bisect_right(self.lines, start), 'kind':kind, 'url':url,
                           '_start':start, '_end':end, '_mode':mode, '_prefix':prefix, '_suffix':suffix, '_quote':quote})

    def javascript(self, text, offset=0, mapping=None):
        tokens = _tokens(text)
        self.token_count += len(tokens)
        if self.token_count > MAX_SCAN_TOKENS:
            raise ValueError('源码过于复杂，超过静态扫描词元限制；请使用源码编辑器核对')
        scopes, parents, braces, bindings = [], {0:None}, {}, {0:set()}
        current, next_scope, nesting = 0, 0, 0
        for index, token in enumerate(tokens):
            scopes.append(current)
            if token.value == '{':
                nesting += 1
                if nesting > MAX_EXPRESSION_TOKENS:
                    raise ValueError('源码嵌套过于复杂；请使用源码编辑器核对')
                next_scope += 1
                parents[next_scope], bindings[next_scope], braces[index] = current, set(), next_scope
                current = next_scope
            elif token.value == '}' and parents[current] is not None:
                nesting -= 1
                current = parents[current]
        # Parameters and declarations shadow browser globals only in their scope.
        for index, token in enumerate(tokens):
            if token.value in ('const', 'let', 'var', 'class', 'function') and index + 1 < len(tokens) and tokens[index+1].kind == 'identifier':
                bindings[scopes[index]].add(tokens[index+1].value)
            if token.value == 'function':
                opening = index + 1
                while opening < len(tokens) and tokens[opening].value not in ('(', '{', ';'):
                    if opening - index > MAX_EXPRESSION_TOKENS:
                        raise ValueError('源码表达式过于复杂；请使用源码编辑器核对')
                    opening += 1
                if opening >= len(tokens) or tokens[opening].value != '(':
                    continue
                end, depth = opening + 1, 1
                while end < len(tokens) and depth:
                    if end - opening > MAX_EXPRESSION_TOKENS:
                        raise ValueError('源码表达式过于复杂；请使用源码编辑器核对')
                    depth += (tokens[end].value == '(') - (tokens[end].value == ')')
                    end += 1
                if end in braces:
                    bindings[braces[end]].update(item.value for item in tokens[opening+1:end-1] if item.kind == 'identifier')
            elif token.value == '=>':
                beginning = index - 1
                if beginning >= 0 and tokens[beginning].value == ')':
                    depth = 1; beginning -= 1
                    while beginning >= 0 and depth:
                        if index - beginning > MAX_EXPRESSION_TOKENS:
                            raise ValueError('源码表达式过于复杂；请使用源码编辑器核对')
                        depth += (tokens[beginning].value == ')') - (tokens[beginning].value == '(')
                        beginning -= 1
                    parameters = tokens[beginning+2:index-1]
                else:
                    parameters = tokens[max(0, beginning):index]
                names = {item.value for item in parameters if item.kind == 'identifier'}
                if index + 1 in braces:
                    bindings[braces[index+1]].update(names)
                else:
                    next_scope += 1
                    parents[next_scope], bindings[next_scope] = scopes[index], names
                    end = _expression_end(tokens, index + 1, text)
                    scopes[index+1:end] = [next_scope] * (end - index - 1)
        definitions = {scope:{} for scope in parents}
        for index, token in enumerate(tokens[:-1]):
            if token.kind == 'identifier' and tokens[index + 1].value == '=' and (index == 0 or tokens[index-1].value not in ('.', ']')):
                end = _expression_end(tokens, index + 2, text)
                definitions[scopes[index]].setdefault(token.value, []).append(tokens[index + 2:end])

        visible_cache = {}
        def visible(scope):
            if scope in visible_cache:
                return visible_cache[scope]
            initial = scope
            chain, result, shadows = [], {}, set()
            while scope is not None:
                chain.append(scope); scope = parents[scope]
            for scope in reversed(chain):
                result.update({name:None for name in bindings[scope]})
                result.update({name:values[0] if len(values) == 1 else None for name, values in definitions[scope].items()})
                shadows.update(bindings[scope])
            visible_cache[initial] = result, shadows
            return result, shadows

        def dom_reference(expression):
            if not expression:
                return False
            member, end = _member(expression, 0)
            if member not in {lookup + '()' for lookup in DOM_LOOKUPS} or end != len(expression):
                return False
            opening = next(index for index, item in enumerate(expression) if item.value == '(')
            argument = expression[opening+1:-1]
            if len(argument) != 1 or argument[0].kind != 'string':
                return False
            selector = _string(argument[0].value)
            if selector is None:
                return False
            if member == 'document.createElement()':
                return selector.lower() in ('a', 'area')
            if member == 'document.querySelector()' and re.match(r'\s*(?:link|base|img|script|source|video|audio|iframe)\b', selector, re.I):
                return False
            return True

        def collect(expression, kind, constants):
            expression = _unparen(expression)
            if not expression:
                return
            # Preserve conditional branches and list each static alternative.
            depth, question, nested = 0, None, 0
            for index, token in enumerate(expression):
                value = token.value
                if value in ('(', '[', '{'): depth += 1
                elif value in (')', ']', '}'): depth -= 1
                elif not depth and value == '?':
                    if question is None: question = index
                    else: nested += 1
                elif not depth and value == ':' and question is not None:
                    if nested: nested -= 1
                    else:
                        collect(expression[question+1:index], kind, constants)
                        collect(expression[index+1:], kind, constants)
                        return
            resolved = _resolve(expression, constants)
            if not _url(resolved):
                self.warnings.append(f'{self.path}:{bisect.bisect_right(self.lines, offset + expression[0].start)} 有无法静态解析的跳转，请在源码编辑器核对。')
                return
            start, end = expression[0].start, expression[-1].end
            if mapping:
                start, end = mapping[0][start], mapping[1][end-1]
            self.add(offset + start, offset + end, resolved,
                     'js_variable' if any(t.kind == 'identifier' for t in expression) else kind,
                     mode='js_html' if mapping else 'js')

        for index, token in enumerate(tokens):
            if token.kind != 'identifier' or (index and tokens[index-1].value in ('.', '?')):
                continue
            member, end = _member(tokens, index)
            assignment = member in NAVIGATION or member.endswith('.href') and member[:-5] in NAVIGATION
            call = member in {'open', 'window.open', 'top.open', 'self.open', 'parent.open'} or any(member == root + method for root in NAVIGATION for method in ('.assign', '.replace'))
            if not assignment and not call and not member.endswith(('.href', '.setAttribute')):
                continue
            constants, shadows = visible(scopes[index])
            attribute_call = False
            if member.endswith(('.href', '.setAttribute')) and 'document' not in shadows:
                base = member.rsplit('.', 1)[0]
                # DOM lookups consume the call; a stored element binding can be
                # resolved without changing its source declaration or API URLs.
                dom = dom_reference(tokens[index:end-2]) if base.endswith('()') else dom_reference(constants.get(base))
                if dom:
                    assignment |= member.endswith('.href')
                    attribute_call = member.endswith('.setAttribute')
                    call |= attribute_call
            if end < len(tokens) and ((assignment and tokens[end].value == '=') or (call and tokens[end].value == '(')):
                if member.split('.')[0] in shadows & {'window', 'location', 'document', 'open', 'top', 'self', 'parent'}:
                    self.warnings.append(f'{self.path}:{bisect.bisect_right(self.lines, offset + token.start)} 使用局部同名对象，请在源码编辑器核对跳转。')
                    continue
                beginning = end + 1
                if attribute_call:
                    if beginning+1 >= len(tokens) or tokens[beginning].kind != 'string' or _string(tokens[beginning].value) != 'href' or tokens[beginning+1].value != ',':
                        continue
                    beginning += 2
                finish = _expression_end(tokens, beginning, text)
                collect(tokens[beginning:finish], 'js_location', constants)


class PageParser(HTMLParser):
    def __init__(self, scanner):
        super().__init__(convert_charrefs=False)
        self.scanner, self.script, self.inert = scanner, None, None

    def handle_starttag(self, tag, attrs):
        if self.inert:
            return
        if tag in ('textarea', 'title', 'xmp', 'noscript'):
            self.inert = tag
            return
        row, col = self.getpos()
        offset = self.scanner.lines[row-1] + col
        raw = self.get_starttag_text()
        values = {}
        for match in ATTR.finditer(raw):
            group = next(i for i in (2, 3, 4) if match.group(i) is not None)
            name, value = match.group(1).lower(), match.group(group)
            values.setdefault(name, (value, offset + match.start(group), offset + match.end(group), group != 4))
        for name, (value, start, end, quoted) in values.items():
            if tag in ('a', 'area') and name == 'href' and html.unescape(value).lower().startswith('javascript:'):
                decoded, starts, ends = _decoded(value)
                self.scanner.javascript(decoded, start, (starts, ends))
            elif (tag in ('a', 'area') and name == 'href') or (tag == 'form' and name == 'action') or (tag in ('button', 'input') and name == 'formaction'):
                self.scanner.add(start, end, html.unescape(value), 'anchor', 'html', quote=quoted)
            elif name.startswith('on'):
                decoded, starts, ends = _decoded(value)
                self.scanner.javascript(decoded, start, (starts, ends))
        if tag == 'meta' and html.unescape(values.get('http-equiv', ('',))[0]).lower() == 'refresh' and 'content' in values:
            raw_value, start, end, quoted = values['content']
            value = html.unescape(raw_value)
            match = re.fullmatch(r'''(\s*[0-9.]+\s*;\s*url\s*=\s*)([\s\S]+)''', value, re.I)
            if match:
                target = match.group(2).strip()
                surrounding = target[0] if len(target) >= 2 and target[0] in "\"'" and target[-1] == target[0] else ''
                self.scanner.add(start, end, target[1:-1] if surrounding else target, 'meta_refresh', 'html', match.group(1) + surrounding, surrounding, quoted)
        if tag == 'script' and 'src' not in values and (dict(attrs).get('type') or '').lower() in ('', 'text/javascript', 'application/javascript', 'module'):
            self.script = offset + len(raw)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if self.inert:
            if self.inert == tag:
                self.inert = None
            return
        if tag == 'script' and self.script is not None:
            row, col = self.getpos()
            end = self.scanner.lines[row-1] + col
            self.scanner.javascript(self.scanner.text[self.script:end], self.script)
            self.script = None


def scan_bundle(root, version_id, snapshots=None):
    files, occurrences, warnings, token_count = source_files(root), [], [], 0
    for path, (file, _) in sorted(files.items()):
        if Path(path).suffix.lower() not in ('.html', '.htm', '.js', '.mjs'):
            continue
        payload = read_source(file, path)
        if snapshots is not None:
            snapshots[path] = payload
        scanner = Scanner(path, payload.decode('utf-8-sig'))
        if Path(path).suffix.lower() in ('.html', '.htm'):
            PageParser(scanner).feed(scanner.text)
        else:
            scanner.javascript(scanner.text)
        token_count += scanner.token_count
        if token_count > MAX_SCAN_TOKENS:
            raise ValueError('源码包过于复杂，超过静态扫描词元限制；请使用源码编辑器核对')
        fingerprint = hashlib.sha256(payload).hexdigest()
        previous = -1
        for ordinal, item in enumerate(sorted(scanner.spans, key=lambda span:span['_start'])):
            if item['_start'] < previous:
                raise ValueError('跳转位置重叠，请使用源码编辑器核对')
            previous = item['_end']
            item['key'] = f'{path}:{ordinal}'
            item['id'] = hashlib.sha256(f"{version_id}:{path}:{fingerprint}:{item['_start']}:{item['_end']}".encode()).hexdigest()
            occurrences.append(item)
        warnings.extend(scanner.warnings)
        if len(occurrences) > MAX_OCCURRENCES:
            raise ValueError('跳转位置超过 5000 处，请缩小源码包后重试')
    return occurrences, list(dict.fromkeys(warnings))[:100]


def public_occurrences(occurrences):
    return [{key:value for key, value in item.items() if not key.startswith('_')} for item in occurrences]


def replace_bundle(root, version_id, occurrence_ids, url, pages, filename):
    # Scan again so changes while checking a link cannot publish stale offsets.
    snapshots = {}
    occurrences, _ = scan_bundle(root, version_id, snapshots)
    wanted = set(occurrence_ids)
    if len(wanted) != len(occurrence_ids) or not wanted or not wanted.issubset({item['id'] for item in occurrences}):
        from .store import Conflict
        raise Conflict('源码或跳转位置已变化，请重新扫描后再换链')
    selected = [item for item in occurrences if item['id'] in wanted]
    files, total, edited, digest = source_files(root), 0, [], hashlib.sha256()
    for path, (file, _) in sorted(files.items()):
        data = snapshots[path] if path in snapshots else read_source(file, path)
        positions = [item for item in selected if item['path'] == path]
        if positions:
            text = data.decode('utf-8-sig')
            for item in sorted(positions, key=lambda span:span['_start'], reverse=True):
                if item['_mode'] == 'html':
                    replacement = html.escape(item['_prefix'] + url + item['_suffix'], quote=True)
                    if not item['_quote']:
                        replacement = '"' + replacement + '"'
                else:
                    replacement = json.dumps(url, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                    if item['_mode'] == 'js_html':
                        replacement = html.escape(replacement, quote=True)
                text = text[:item['_start']] + replacement + text[item['_end']:]
            data = (b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
        validate_file(path, data)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError('源码目录超过总大小限制')
        edited.append((path, data))
        digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    return write_bundle(edited, filename, pages, digest.hexdigest()), len(selected)
