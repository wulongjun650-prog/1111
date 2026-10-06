"""Static navigation spans only. Never execute source or replace arbitrary URLs."""
import bisect
from dataclasses import dataclass
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

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


def _query(pairs):
    """Encode separators once and leave the readable text, including Chinese, visible."""
    def component(value):
        encoded = []
        for char in str(value):
            if ord(char) < 32 or char in '&=#%+?':
                encoded.append(quote(char, safe=''))
            elif char == ' ':
                encoded.append('%20')
            else:
                encoded.append(char)
        return ''.join(encoded)
    return '&'.join(f'{component(key)}={component(value)}' for key, value in pairs.items())


def split_plus(expression):
    """Split a + chain without treating a.b or a call as several operands."""
    depth, start, parts, found = 0, 0, [], False
    for index, token in enumerate(expression):
        if token.value in ('(', '[', '{'):
            depth += 1
        elif token.value in (')', ']', '}'):
            depth -= 1
        elif not depth and token.value == '+':
            parts.append(expression[start:index])
            start = index + 1
            found = True
    if not found:
        return []
    parts.append(expression[start:])
    return parts


def _template_text(raw):
    return raw.replace('\\`', '`').replace('\\/', '/').replace('\\\\', '\\')


def _template_url(token):
    """Static prefix of a template literal that already names a host."""
    if token.kind != 'string' or len(token.value) < 2 or token.value[0] != '`' or '${' not in token.value:
        return None
    prefix = _template_text(token.value[1:].split('${', 1)[0])
    if not _url(prefix):
        return None
    try:
        parts = urlsplit(prefix)
    except ValueError:
        return None
    if parts.scheme.lower() not in ('http', 'https', 'whatsapp', 'bandapp') or not parts.netloc:
        return None
    return prefix


def _template_pieces(body):
    pieces, index = [], 0
    while index < len(body):
        mark = body.find('${', index)
        if mark < 0:
            pieces.append(('text', body[index:]))
            break
        if mark > index:
            pieces.append(('text', body[index:mark]))
        depth, cursor = 1, mark + 2
        while cursor < len(body) and depth:
            depth += (body[cursor] == '{') - (body[cursor] == '}')
            cursor += 1
        if depth:
            return None
        pieces.append(('expr', body[mark + 2:cursor - 1]))
        index = cursor
    return pieces


TRACKER_HOSTS = ('googletagmanager.com', 'google-analytics.com', 'googleadservices.com', 'googlesyndication.com', 'doubleclick.net', 'googleapis.com', 'connect.facebook.net')


def _tracker(url):
    """Analytics and tag endpoints are not visitor jumps."""
    try:
        parts = urlsplit(url if '://' in url else '//' + url)
    except ValueError:
        return False
    host = parts.netloc.lower().split('@')[-1].split(':')[0]
    if host.startswith('www.'):
        host = host[4:]
    if host in TRACKER_HOSTS or any(host.endswith('.' + name) for name in TRACKER_HOSTS):
        return True
    return host == 'facebook.com' and parts.path.rstrip('/').endswith('/tr')


def _whatsapp_url(value):
    """True when the resolved target is a WhatsApp chat, not an ordinary link."""
    if not isinstance(value, str) or not _url(value):
        return False
    try:
        parts = urlsplit(value if '://' in value else '//' + value)
    except ValueError:
        return False
    if parts.scheme.lower() == 'whatsapp':
        return True
    host = parts.netloc.lower().split('@')[-1].split(':')[0]
    if host.startswith('www.'):
        host = host[4:]
    return host == 'wa.me' or host == 'whatsapp.com' or host.endswith('.whatsapp.com')


def _phone_digits(token):
    """Digits of a phone literal. A full https URL is not a phone."""
    if token is None or getattr(token, 'kind', '') != 'string':
        return None
    raw = _string(token.value)
    if raw is None or not re.fullmatch(r'\+?[\d\s().-]{8,24}', raw.strip()):
        return None
    digits = re.sub(r'\D', '', raw)
    return digits if 8 <= len(digits) <= 15 else None


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
        if not _url(url) or _tracker(url):
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
        # Plain functions and arrows that build a URL and hand it to location.
        # Only the returned expression is replaceable; the call itself is not.
        function_bodies = {}
        token_index = {id(token): index for index, token in enumerate(tokens)}

        def store_function(scope, name, body):
            bucket = function_bodies.setdefault(scope, {})
            bucket[name] = None if name in bucket else body

        def params_end(start):
            if start >= len(tokens) or tokens[start].value != '(':
                return None
            depth, index = 1, start + 1
            while index < len(tokens) and depth:
                if index - start > MAX_EXPRESSION_TOKENS:
                    return None
                depth += (tokens[index].value == '(') - (tokens[index].value == ')')
                index += 1
            return index if not depth else None

        def parameter_names(parameters):
            names, index, depth, expect = [], 0, 0, True
            while index < len(parameters):
                token = parameters[index]
                if token.value in ('(', '[', '{'):
                    depth += 1
                elif token.value in (')', ']', '}'):
                    depth -= 1
                elif not depth and token.value == ',':
                    expect = True
                elif not depth and token.value == '=':
                    expect = False
                elif not depth and expect and token.kind == 'identifier':
                    names.append(token.value)
                    expect = False
                index += 1
            return names

        def block_body(brace):
            depth, index = 1, brace + 1
            start = index
            while index < len(tokens) and depth:
                depth += (tokens[index].value == '{') - (tokens[index].value == '}')
                index += 1
            return None if depth else tokens[start:index - 1]

        def find_arrow(start):
            depth = 0
            for index in range(start, min(len(tokens), start + MAX_EXPRESSION_TOKENS)):
                value = tokens[index].value
                if value in ('(', '[', '{'):
                    depth += 1
                elif value in (')', ']', '}'):
                    depth -= 1
                elif not depth and value == '=>':
                    return index
                elif not depth and value in (';', '{', '='):
                    return None
            return None

        def arrow_parameters(arrow):
            beginning = arrow - 1
            if beginning >= 0 and tokens[beginning].value == ')':
                depth = 1
                beginning -= 1
                while beginning >= 0 and depth:
                    depth += (tokens[beginning].value == ')') - (tokens[beginning].value == '(')
                    beginning -= 1
                return tokens[beginning + 2:arrow - 1]
            return tokens[max(0, beginning):arrow]

        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.value == 'function' and index + 1 < len(tokens) and tokens[index + 1].kind == 'identifier':
                end = params_end(index + 2)
                if end is not None and end < len(tokens) and tokens[end].value == '{':
                    body = block_body(end)
                    if body is not None:
                        store_function(scopes[index], tokens[index + 1].value, ('block', body, parameter_names(tokens[index + 3:end - 1])))
            elif token.value in ('const', 'let', 'var') and index + 3 < len(tokens) and tokens[index + 1].kind == 'identifier' and tokens[index + 2].value == '=':
                cursor = index + 3
                if tokens[cursor].value == 'async' and cursor + 1 < len(tokens):
                    cursor += 1
                if tokens[cursor].value == 'function':
                    params_at = cursor + 2 if cursor + 1 < len(tokens) and tokens[cursor + 1].kind == 'identifier' else cursor + 1
                    end = params_end(params_at)
                    if end is not None and end < len(tokens) and tokens[end].value == '{':
                        body = block_body(end)
                        if body is not None:
                            store_function(scopes[index], tokens[index + 1].value, ('block', body, parameter_names(tokens[params_at + 1:end - 1])))
                else:
                    arrow = find_arrow(cursor)
                    if arrow is not None and arrow + 1 < len(tokens):
                        names = parameter_names(arrow_parameters(arrow))
                        if tokens[arrow + 1].value == '{':
                            body = block_body(arrow + 1)
                            if body is not None:
                                store_function(scopes[index], tokens[index + 1].value, ('block', body, names))
                        else:
                            finish = _expression_end(tokens, arrow + 1, text)
                            expression = tokens[arrow + 1:finish]
                            if expression:
                                store_function(scopes[index], tokens[index + 1].value, ('expr', expression, names))
            index += 1

        def lookup_function(name, scope):
            while scope is not None:
                bodies = function_bodies.get(scope, {})
                if name in bodies:
                    return bodies[name]
                if name in bindings.get(scope, ()):
                    return None
                scope = parents[scope]
            return None

        def function_returns(body):
            kind, nodes, _params = body
            if kind == 'expr':
                yield nodes
                return
            nested, paren, pending, index = [], 0, False, 0
            while index < len(nodes):
                value = nodes[index].value
                if value == '(':
                    paren += 1
                elif value == ')':
                    paren -= 1
                if value == 'return' and not paren and 'fn' not in nested:
                    finish = _expression_end(nodes, index + 1, text)
                    yield nodes[index + 1:finish]
                    index = finish
                    continue
                if not paren and value in ('function', '=>'):
                    pending = True
                elif not paren and value == '{':
                    nested.append('fn' if pending else 'block')
                    pending = False
                elif not paren and value == '}':
                    if nested:
                        nested.pop()
                    pending = False
                elif pending and not paren and value == ';':
                    pending = False
                index += 1

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

        def scope_of(expression, fallback):
            if not expression:
                return fallback
            return scopes[token_index[id(expression[0])]]

        def object_fields(expression):
            expression = _unparen(expression)
            if not expression or expression[0].value != '{' or expression[-1].value != '}':
                return {}
            body, fields, index = expression[1:-1], {}, 0
            while index < len(body):
                key_token = body[index]
                key = key_token.value if key_token.kind == 'identifier' else _string(key_token.value) if key_token.kind == 'string' else None
                if key is None or index + 1 >= len(body) or body[index + 1].value != ':':
                    break
                finish = _expression_end(body, index + 2, text)
                fields[key] = body[index + 2:finish]
                index = finish + 1 if finish < len(body) and body[finish].value == ',' else finish
            return fields

        def split_or(expression):
            depth, start, parts = 0, 0, []
            for index, token in enumerate(expression):
                if token.value in ('(', '[', '{'):
                    depth += 1
                elif token.value in (')', ']', '}'):
                    depth -= 1
                elif not depth and token.value == '||':
                    parts.append(expression[start:index])
                    start = index + 1
            if parts:
                parts.append(expression[start:])
            return parts

        def call_end(expression):
            if len(expression) < 3 or expression[1].value != '(':
                return None
            depth = 0
            for index, token in enumerate(expression[1:], 1):
                depth += (token.value == '(') - (token.value == ')')
                if not depth:
                    return index
            return None

        def field_at(expression, index):
            if index < len(expression) and expression[index].value == '.' and index + 1 < len(expression) and expression[index + 1].kind == 'identifier':
                return expression[index + 1].value, index + 2
            if index + 2 < len(expression) and expression[index].value == '[' and expression[index + 1].kind == 'string' and expression[index + 2].value == ']':
                return _string(expression[index + 1].value), index + 3
            return None, index

        def static_object(expression, scope, seen, extra):
            expression = _unparen(expression)
            if not expression or len(seen) > 12:
                return None
            direct = object_fields(expression)
            if direct:
                return direct
            alternatives = split_or(expression)
            if alternatives:
                for part in alternatives:
                    found = static_object(part, scope, seen, extra)
                    if found:
                        return found
                return None
            called = call_name(expression)
            if called and called not in seen and len(seen) < 8:
                body = lookup_function(called, scope)
                if body:
                    # A helper can return {scheme, universal}; the caller jumps through one field.
                    overlay = dict(extra or {})
                    for param, argument in zip(body[2], call_arguments(expression)):
                        if argument:
                            overlay[param] = argument
                    for item in function_returns(body):
                        found = static_object(item, scope_of(item, scope), (*seen, called), overlay)
                        if found:
                            return found
                    return None
            if expression[0].kind != 'identifier' or expression[0].value in seen:
                return None
            name = expression[0].value
            constants, _shadows = visible(scope)
            if extra and name in extra:
                root, root_scope, root_extra = extra[name], scope_of(extra[name], scope), None
            else:
                defined = constants.get(name)
                if not defined:
                    return None
                root, root_scope, root_extra = defined, scope_of(defined, scope), extra
            obj = static_object(root, root_scope, (*seen, name), root_extra)
            index = 1
            while obj is not None and index < len(expression):
                field, index = field_at(expression, index)
                if not field:
                    return None
                chosen = obj.get(field)
                if not chosen:
                    return None
                obj = static_object(chosen, scope_of(chosen, root_scope), (*seen, name), root_extra)
            return obj

        def static_eval(expression, scope, seen=(), extra=None):
            expression = _unparen(expression)
            if not expression or len(seen) > 12:
                return None
            if expression[0].value in ('String', 'encodeURIComponent') and call_end(expression) == len(expression) - 1:
                return static_eval(expression[2:-1], scope, seen, extra)
            for index, token in enumerate(expression):
                if token.value != '.' or index + 5 >= len(expression) or expression[index + 1].value != 'replace' or expression[index + 2].value != '(':
                    continue
                pattern = text[expression[index + 3].start:expression[index + 3].end]
                if expression[index + 3].value != '<regex>' or pattern not in ('/\\D/g', '/\\D/gi', '/[^0-9]/g') or expression[index + 4].value != ',':
                    continue
                if expression[index + 5].kind != 'string' or _string(expression[index + 5].value) != '':
                    continue
                base = static_eval(expression[:index], scope, seen, extra)
                return None if base is None else re.sub(r'\D', '', base)
            alternatives = split_or(expression)
            if alternatives:
                for part in alternatives:
                    value = static_eval(part, scope, seen, extra)
                    if value:
                        return value
                return ''
            parts = split_plus(expression)
            if parts:
                built, complete = [], True
                for part in parts:
                    value = static_eval(part, scope, seen, extra)
                    if value is None:
                        complete = False
                        break
                    built.append(value)
                    if sum(map(len, built)) > 4096:
                        return None
                if not built or (not complete and not any(built)):
                    return None
                return ''.join(built)
            if len(expression) == 1 and expression[0].kind == 'string':
                return _string(expression[0].value)
            constants, _shadows = visible(scope)
            if len(expression) == 1 and expression[0].kind == 'identifier' and expression[0].value not in seen:
                name = expression[0].value
                if extra and name in extra:
                    return static_eval(extra[name], scope_of(extra[name], scope), (*seen, name))
                defined = constants.get(name)
                return static_eval(defined, scope_of(defined, scope), (*seen, name), extra) if defined else None
            if expression[0].kind != 'identifier' or expression[0].value in seen or field_at(expression, 1)[0] is None:
                return None
            name = expression[0].value
            if extra and name in extra:
                root, root_scope, root_extra = extra[name], scope_of(extra[name], scope), None
            else:
                defined = constants.get(name)
                if not defined:
                    return None
                root, root_scope, root_extra = defined, scope_of(defined, scope), extra
            obj = static_object(root, root_scope, (*seen, name), root_extra)
            index = 1
            while obj is not None and index < len(expression):
                field, index = field_at(expression, index)
                if not field:
                    return None
                chosen = obj.get(field)
                if not chosen:
                    return None
                if index >= len(expression):
                    return static_eval(chosen, scope_of(chosen, root_scope), (*seen, name), root_extra)
                obj = static_object(chosen, scope_of(chosen, root_scope), (*seen, name), root_extra)
            return None

        def origin_scope(expression, fallback):
            # Re-lexing a template piece makes tokens that are not in token_index.
            if not expression:
                return fallback
            index = token_index.get(id(expression[0]))
            return scopes[index] if index is not None else fallback

        def literal_token(expression, scope, seen=(), extra=None):
            """Original string token that feeds a phone, or None."""
            expression = _unparen(expression)
            if not expression or len(seen) > 12:
                return None
            if expression[0].value in ('String', 'encodeURIComponent') and call_end(expression) == len(expression) - 1:
                return literal_token(expression[2:-1], scope, seen, extra)
            for index, token in enumerate(expression):
                if token.value != '.' or index + 5 >= len(expression) or expression[index + 1].value != 'replace' or expression[index + 2].value != '(':
                    continue
                pattern = text[expression[index + 3].start:expression[index + 3].end] if id(expression[index + 3]) in token_index else ''
                if expression[index + 3].value != '<regex>' or pattern not in ('/\\D/g', '/\\D/gi', '/[^0-9]/g') or expression[index + 4].value != ',':
                    continue
                if expression[index + 5].kind != 'string' or _string(expression[index + 5].value) != '':
                    continue
                return literal_token(expression[:index], scope, seen, extra)
            alternatives = split_or(expression)
            if alternatives:
                for part in alternatives:
                    if static_eval(part, scope, seen, extra):
                        return literal_token(part, scope, seen, extra)
                return None
            if len(expression) == 1 and expression[0].kind == 'string' and id(expression[0]) in token_index and _string(expression[0].value) is not None:
                return expression[0]
            constants, _shadows = visible(scope)
            if len(expression) == 1 and expression[0].kind == 'identifier' and expression[0].value not in seen:
                name = expression[0].value
                if extra and name in extra:
                    argument = extra[name]
                    return literal_token(argument, origin_scope(argument, scope), (*seen, name))
                defined = constants.get(name)
                return literal_token(defined, origin_scope(defined, scope), (*seen, name), extra) if defined else None
            if expression[0].kind != 'identifier' or expression[0].value in seen or field_at(expression, 1)[0] is None:
                return None
            name = expression[0].value
            if extra and name in extra:
                root, root_scope, root_extra = extra[name], origin_scope(extra[name], scope), None
            else:
                defined = constants.get(name)
                if not defined:
                    return None
                root, root_scope, root_extra = defined, origin_scope(defined, scope), extra
            obj = static_object(root, root_scope, (*seen, name), root_extra)
            index = 1
            while obj is not None and index < len(expression):
                field, index = field_at(expression, index)
                if not field:
                    return None
                chosen = obj.get(field)
                if not chosen:
                    return None
                if index >= len(expression):
                    return literal_token(chosen, origin_scope(chosen, root_scope), (*seen, name), root_extra)
                obj = static_object(chosen, origin_scope(chosen, root_scope), (*seen, name), root_extra)
            return None

        def is_url_params(expression):
            expression = _unparen(expression or [])
            return len(expression) >= 4 and expression[0].value == 'new' and expression[1].value == 'URLSearchParams' and expression[2].value == '(' and expression[-1].value == ')'

        def params_template(token, scope, prefix, pieces, extra):
            expressions = [piece for kind, piece in pieces if kind == 'expr']
            if len(expressions) != 1 or not prefix.endswith('?'):
                return None
            parsed = _tokens(expressions[0].strip())
            name = parsed[0].value if parsed and parsed[0].kind == 'identifier' and (len(parsed) == 1 or (len(parsed) >= 3 and parsed[1].value == '.' and parsed[2].value == 'toString')) else None
            constants, _shadows = visible(scope)
            if not name or not is_url_params(constants.get(name)):
                return None
            found, phone_token, limit = {}, None, token_index[id(token)]
            for index, item in enumerate(tokens):
                if index >= limit or scopes[index] != scope or item.value != name:
                    continue
                if index + 6 >= len(tokens) or tokens[index + 1].value != '.' or tokens[index + 2].value != 'set' or tokens[index + 3].value != '(':
                    continue
                key_token = tokens[index + 4]
                if key_token.kind != 'string' or tokens[index + 5].value != ',':
                    continue
                key = _string(key_token.value)
                finish = _expression_end(tokens, index + 6, text)
                if not key:
                    continue
                value_tokens = tokens[index + 6:finish]
                if key == 'phone':
                    phone_token = literal_token(value_tokens, scope, (), extra)
                value = static_eval(value_tokens, scope, (), extra)
                if value:
                    found[key] = value
                else:
                    found.pop(key, None)
            query = _query(found)
            url = prefix + query if query else prefix
            return url, phone_token if _whatsapp_url(prefix) else None

        def fill_template(pieces, scope, extra):
            built, complete, phone_token = [], True, None
            for kind, piece in pieces:
                if kind == 'text':
                    built.append(_template_text(piece))
                    continue
                parsed = _tokens(piece.strip())
                value = static_eval(parsed, scope, (), extra) if parsed else None
                if value is None:
                    complete = False
                    break
                built.append(value)
                candidate = literal_token(parsed, scope, (), extra) if parsed else None
                if _phone_digits(candidate):
                    phone_token = candidate
                if sum(map(len, built)) > 4096:
                    return None
            if not built:
                return None
            url = ''.join(built)
            if not complete:
                url = re.sub(r'[?&][^?&#=]*=?$', '', url).rstrip('?&=')
            if any(ord(char) < 32 for char in url):
                url = ''.join(quote(char, safe='') if ord(char) < 32 else char for char in url)
            if not _url(url):
                return None
            return url, phone_token if _whatsapp_url(url) else None

        def expand_template(token, scope, extra=None):
            prefix = _template_url(token)
            if not prefix:
                return None
            pieces = _template_pieces(token.value[1:-1])
            if not pieces:
                return prefix, None
            candidates = [item for item in (params_template(token, scope, prefix, pieces, extra), fill_template(pieces, scope, extra)) if item and item[0]]
            if not candidates:
                return prefix, None
            url, phone_token = max(candidates, key=lambda item: len(item[0]))
            return url, phone_token if _whatsapp_url(url) else None

        def partial_location(expression, scope, extra):
            expression = _unparen(expression)
            parts = split_plus(expression)
            if not parts:
                return None
            built, complete, phone_token = [], True, None
            for part in parts:
                value = static_eval(part, scope, (), extra)
                part_phone = None
                if value is None and len(part) == 1:
                    expanded = expand_template(part[0], scope, extra)
                    if expanded:
                        value, part_phone = expanded
                if value is None:
                    complete = False
                    break
                built.append(value)
                if part_phone is None:
                    part_phone = literal_token(part, scope, (), extra)
                if _phone_digits(part_phone):
                    phone_token = part_phone
                if sum(map(len, built)) > 4096:
                    return None
            if not built or (not complete and not any(built)):
                return None
            url = ''.join(built)
            if not complete:
                url = re.sub(r'[?&][^?&#=]*=?$', '', url).rstrip('?&=')
            if not _url(url):
                return None
            return url, phone_token if _whatsapp_url(url) else None

        def call_name(expression):
            expression = _unparen(expression)
            if len(expression) < 3 or expression[0].kind != 'identifier' or expression[1].value != '(':
                return None
            depth = 0
            for index, token in enumerate(expression):
                depth += (token.value == '(') - (token.value == ')')
                if index and not depth:
                    return expression[0].value if index == len(expression) - 1 and token.value == ')' else None
            return None

        def call_arguments(expression):
            expression = _unparen(expression)
            body = expression[2:-1]
            if not body:
                return []
            args, start, depth = [], 0, 0
            for index, token in enumerate(body):
                if token.value in ('(', '[', '{'):
                    depth += 1
                elif token.value in (')', ']', '}'):
                    depth -= 1
                elif not depth and token.value == ',':
                    args.append(body[start:index])
                    start = index + 1
            args.append(body[start:])
            return args

        def collect(expression, kind, scope, seen=(), extra=None):
            expression = _unparen(expression)
            if not expression:
                return False
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
                        collect(expression[question+1:index], kind, scope, seen, extra)
                        collect(expression[index+1:], kind, scope, seen, extra)
                        return True
            constants, _shadows = visible(scope)
            resolved = _resolve(expression, constants)
            expanded = expand_template(expression[0], scope, extra) if len(expression) == 1 else None
            template, phone_token = expanded if expanded else (None, None)
            if not _url(resolved) and template:
                resolved = template
            else:
                phone_token = None
            if not _url(resolved):
                joined = partial_location(expression, scope, extra)
                if joined and _url(joined[0]):
                    resolved, phone_token = joined
            if _url(resolved):
                digits = _phone_digits(phone_token) if phone_token and _whatsapp_url(resolved) else None
                if digits:
                    # The built chat URL stays in source. Only the phone literal is replaceable.
                    start, end = phone_token.start, phone_token.end
                else:
                    digits = None
                    start, end = expression[0].start, expression[-1].end
                if mapping:
                    start, end = mapping[0][start], mapping[1][end-1]
                if digits:
                    self.add(offset + start, offset + end, digits, 'whatsapp_number',
                             mode='js_html' if mapping else 'js')
                else:
                    dynamic = template or any(token.kind == 'identifier' for token in expression)
                    self.add(offset + start, offset + end, resolved,
                             'js_variable' if dynamic else kind,
                             mode='js_html' if mapping else 'js')
                return True
            if len(expression) == 1 and expression[0].kind == 'identifier' and expression[0].value not in seen:
                name = expression[0].value
                if extra and name in extra:
                    argument = extra[name]
                    return collect(argument, kind, scope_of(argument, scope), (*seen, name))
                defined = constants.get(name)
                if defined:
                    return collect(defined, kind, scope_of(defined, scope), (*seen, name), extra)
            name = call_name(expression)
            if name and name not in seen and len(seen) < 8:
                body = lookup_function(name, scope)
                returns = list(function_returns(body)) if body else []
                if returns:
                    params = body[2]
                    overlay = {}
                    for param, argument in zip(params, call_arguments(expression)):
                        if argument:
                            overlay[param] = argument
                    found = False
                    for item in returns:
                        found = collect(item, kind, scope_of(item, scope), (*seen, name), overlay) or found
                    return found
            field, next_at = field_at(expression, 1)
            if field and next_at == len(expression) and expression[0].kind == 'identifier' and expression[0].value not in seen:
                name = expression[0].value
                if extra and name in extra:
                    base, base_scope, field_extra = extra[name], scope_of(extra[name], scope), None
                else:
                    base = constants.get(name)
                    base_scope = scope_of(base, scope) if base else scope
                    field_extra = extra
                if base:
                    called = call_name(base)
                    if called and called not in (*seen, name) and len(seen) < 8:
                        body = lookup_function(called, base_scope)
                        if body:
                            field_extra = dict(field_extra or {})
                            for param, argument in zip(body[2], call_arguments(base)):
                                if argument:
                                    field_extra[param] = argument
                    obj = static_object(base, base_scope, (*seen, name), field_extra)
                    chosen = obj.get(field) if obj else None
                    if chosen:
                        return collect(chosen, kind, scope_of(chosen, base_scope), (*seen, name), field_extra)
            self.warnings.append(f'{self.path}:{bisect.bisect_right(self.lines, offset + expression[0].start)} 有无法静态解析的跳转，请在源码编辑器核对。')
            return False

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
                argument = tokens[beginning:finish]
                collect(argument, 'js_location', scope_of(argument, scopes[index]))


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


def rewrite_text(data, positions, url):
    """Replace already-scanned spans in one file. The bytes stay on disk."""
    if not positions:
        return data
    text = data.decode('utf-8-sig')
    for item in sorted(positions, key=lambda span: span['_start'], reverse=True):
        if item['_mode'] == 'html':
            replacement = html.escape(item['_prefix'] + url + item['_suffix'], quote=True)
            if not item['_quote']:
                replacement = '"' + replacement + '"'
        else:
            replacement = json.dumps(url, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            if item['_mode'] == 'js_html':
                replacement = html.escape(replacement, quote=True)
        text = text[:item['_start']] + replacement + text[item['_end']:]
    return (b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')


def replace_bundle(root, version_id, occurrence_ids, url, pages, filename):
    # Scan again so changes while checking a link cannot publish stale offsets.
    snapshots = {}
    occurrences, _ = scan_bundle(root, version_id, snapshots)
    wanted = set(occurrence_ids)
    if len(wanted) != len(occurrence_ids) or not wanted or not wanted.issubset({item['id'] for item in occurrences}):
        from .store import Conflict
        raise Conflict('源码或跳转位置已变化，请重新扫描后再换链')
    selected = [item for item in occurrences if item['id'] in wanted]
    # The same number or link is already in every chosen spot. Keep this version.
    if all(item['url'] == url for item in selected):
        return None, 0
    files, total, edited, digest, changed_bytes = source_files(root), 0, [], hashlib.sha256(), False
    for path, (file, _) in sorted(files.items()):
        data = snapshots[path] if path in snapshots else read_source(file, path)
        positions = [item for item in selected if item['path'] == path]
        if positions:
            rewritten = rewrite_text(data, positions, url)
            changed_bytes = changed_bytes or rewritten != data
            data = rewritten
        validate_file(path, data)
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError('源码目录超过总大小限制')
        edited.append((path, data))
        digest.update(path.encode() + b'\0' + str(len(data)).encode() + b'\0' + data)
    if not changed_bytes:
        return None, 0
    return write_bundle(edited, filename, pages, digest.hexdigest()), len(selected)
