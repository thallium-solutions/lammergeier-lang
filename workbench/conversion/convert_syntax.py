#!/usr/bin/env python3
"""
Convert all .tpy files from Python-style (indentation + def + except)
to C-style (braces + func + catch) syntax.
"""
import re
import sys
from pathlib import Path


def convert_file(content: str) -> str:
    lines = content.split('\n')
    # Pass 1: def -> func, except -> catch
    converted = []
    for line in lines:
        converted.append(_replace_keywords(line))
    # Pass 2: indentation -> braces
    result = _indent_to_braces(converted)
    return '\n'.join(result)


def _replace_keywords(line: str) -> str:
    stripped = line.lstrip()
    indent = line[:len(line) - len(stripped)]
    
    # Don't modify comment-only lines (except expect: lines)
    if stripped.startswith('#'):
        return line
    
    # Replace 'def ' with 'func ' as keyword (not inside strings)
    new_stripped = _kw_replace(stripped, 'def ', 'func ')
    # Replace 'except' with 'catch'  
    new_stripped = _kw_replace(new_stripped, 'except', 'catch')
    
    return indent + new_stripped


def _kw_replace(text: str, old: str, new: str) -> str:
    """Replace keyword avoiding inside strings."""
    result = []
    i = 0
    in_str = None
    while i < len(text):
        c = text[i]
        if in_str is None:
            if c in ('"', "'"):
                triple = text[i:i+3]
                if triple in ('"""', "'''"):
                    in_str = triple
                    result.append(triple)
                    i += 3
                    continue
                in_str = c
                result.append(c)
                i += 1
                continue
            if text[i:i+len(old)] == old:
                before_ok = (i == 0 or not (text[i-1].isalnum() or text[i-1] == '_'))
                after_pos = i + len(old)
                if old.endswith(' '):
                    after_ok = True
                else:
                    after_ok = (after_pos >= len(text) or not (text[after_pos].isalnum() or text[after_pos] == '_'))
                if before_ok and after_ok:
                    result.append(new)
                    i += len(old)
                    continue
            result.append(c)
            i += 1
        else:
            if len(in_str) == 3 and text[i:i+3] == in_str:
                result.append(in_str)
                i += 3
                in_str = None
            elif len(in_str) == 1 and c == in_str and (i == 0 or text[i-1] != '\\'):
                result.append(c)
                i += 1
                in_str = None
            else:
                result.append(c)
                i += 1
    return ''.join(result)


def _get_indent(line: str) -> int:
    n = 0
    for c in line:
        if c == ' ':
            n += 1
        elif c == '\t':
            n += 4
        else:
            break
    return n


def _is_block_opener(stripped: str) -> bool:
    code = stripped.split('#')[0].rstrip()
    if not code.endswith(':'):
        return False
    # Must start with a compound-statement keyword
    kws = ['func ', 'if ', 'elif ', 'for ', 'while ', 'with ', 'class ', 
            'interface ', 'match ', 'case ', 'private ', 'static ', 'async ']
    for kw in kws:
        if code.startswith(kw):
            return True
    if code in ('else:', 'try:', 'finally:') or code.startswith('catch') or code.startswith('else:'):
        return True
    # private/static/async func
    for prefix in ('private ', 'static ', 'async ', 'private static ', 'private async ', 'static async '):
        if code.startswith(prefix + 'func '):
            return True
    return False


def _strip_colon(line: str) -> str:
    """Remove trailing : from block opener line (the code part only)."""
    # Separate comment
    # Find last # not in string (simplified: just split on #)
    parts = line.split('#')
    if len(parts) > 1 and not line.lstrip().startswith('#'):
        code = parts[0]
        comment = '#' + '#'.join(parts[1:])
    else:
        code = line
        comment = ''
    
    rcode = code.rstrip()
    if rcode.endswith(':'):
        rcode = rcode[:-1]
    return rcode + comment


_CONTINUATION_KWS = ('elif ', 'else:', 'else ', 'catch ', 'catch:', 'finally:', 'finally ')


def _is_continuation(stripped: str) -> bool:
    """Check if a line is a continuation keyword (elif/else/catch/finally)."""
    for kw in _CONTINUATION_KWS:
        if stripped.startswith(kw) or stripped == kw.rstrip():
            return True
    return False


def _collect_block_body(lines, i, n, indent):
    """Collect indented body lines starting at i. Returns (body_lines, new_i)."""
    body_lines = []
    # Skip blank lines before body
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        return body_lines, i
    body_indent = _get_indent(lines[i])
    if body_indent <= indent:
        return body_lines, i
    while i < n:
        if not lines[i].strip():
            # Blank line: include if followed by more body
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _get_indent(lines[j]) > indent:
                body_lines.append(lines[i])
                i += 1
            else:
                break
        elif _get_indent(lines[i]) > indent:
            body_lines.append(lines[i])
            i += 1
        else:
            break
    return body_lines, i


def _indent_to_braces(lines: list) -> list:
    result = []
    i = 0
    n = len(lines)
    
    while i < n:
        line = lines[i]
        stripped = line.strip()
        
        if not stripped or stripped.startswith('#'):
            result.append(line)
            i += 1
            continue
        
        if _is_block_opener(stripped):
            indent = _get_indent(line)
            indent_str = ' ' * indent
            opener = _strip_colon(line)
            
            i += 1
            body_lines, i = _collect_block_body(lines, i, n, indent)
            
            # Recursively convert body
            converted_body = _indent_to_braces(body_lines)
            
            result.append(opener + ' {')
            result.extend(converted_body)
            
            # Check if next non-blank line at same indent is a continuation (elif/else/catch/finally)
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _get_indent(lines[j]) == indent and _is_continuation(lines[j].strip()):
                # Chain: } elif/else/catch/finally
                next_stripped = lines[j].strip()
                i = j  # skip to continuation line, will be processed in next iteration
                # But we need to emit "} continuation {" — so instead of closing, 
                # we close and let the next iteration handle it with special merging
                # Actually, let's just merge now: close brace + continuation opener
                next_opener = _strip_colon(lines[j])
                i = j + 1
                next_body, i = _collect_block_body(lines, i, n, indent)
                converted_next = _indent_to_braces(next_body)
                result.append(indent_str + '} ' + next_opener.strip() + ' {')
                result.extend(converted_next)
                
                # Keep chaining (elif chains, catch+finally chains)
                while True:
                    j = i
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and _get_indent(lines[j]) == indent and _is_continuation(lines[j].strip()):
                        cont_opener = _strip_colon(lines[j])
                        i = j + 1
                        cont_body, i = _collect_block_body(lines, i, n, indent)
                        converted_cont = _indent_to_braces(cont_body)
                        result.append(indent_str + '} ' + cont_opener.strip() + ' {')
                        result.extend(converted_cont)
                    else:
                        break
                
                result.append(indent_str + '}')
            else:
                result.append(indent_str + '}')
        else:
            result.append(line)
            i += 1
    
    return result


def process_file(filepath: Path) -> bool:
    content = filepath.read_text(encoding='utf-8')
    new_content = convert_file(content)
    if new_content != content:
        filepath.write_text(new_content, encoding='utf-8')
        return True
    return False


def main():
    project_root = Path(__file__).resolve().parent.parent
    tpy_files = []
    for d in ['tests/cases', 'rosetta_tests', 'examples/basic', 'examples/advanced']:
        dirpath = project_root / d
        if dirpath.is_dir():
            tpy_files.extend(sorted(dirpath.rglob('*.tpy')))
    lib_dir = project_root / 'lib'
    if lib_dir.is_dir():
        tpy_files.extend(sorted(lib_dir.rglob('*.tpy')))
    
    modified = 0
    for f in sorted(tpy_files):
        try:
            if process_file(f):
                modified += 1
        except Exception as e:
            print(f'  ERROR: {f}: {e}')
    
    print(f'{modified} files converted out of {len(tpy_files)} total.')


if __name__ == '__main__':
    main()
