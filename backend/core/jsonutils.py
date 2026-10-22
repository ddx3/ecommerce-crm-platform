import json

# Line/paragraph separator characters. Old Safari treats them as a line break right inside a JS
# literal -> SyntaxError -> the whole inline script fails.
_LINE_SEP = chr(0x2028)  # U+2028
_PARA_SEP = chr(0x2029)  # U+2029


def js_json(obj, **kwargs):
    r"""json.dumps + escaping of characters that are unsafe for inlining in <script>.

    Replaces U+2028/U+2029 with LITERAL escape text (not with the characters themselves - otherwise
    it is a no-op), and also '<','>','&' with \uXXXX, so the data cannot close </script> (broken
    page / stored XSS). The result is valid JSON.
    """
    kwargs.setdefault('ensure_ascii', False)
    return (json.dumps(obj, **kwargs)
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('&', '\\u0026')
            .replace(_LINE_SEP, '\\u2028')
            .replace(_PARA_SEP, '\\u2029'))
