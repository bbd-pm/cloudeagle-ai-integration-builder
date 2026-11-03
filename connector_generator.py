"""Generate a small Python 3.8 API client from selected OpenAPI operations."""
import ast

from ai_generation import operation_method_name


def generate_python(api, selected, auth_env="API_ACCESS_TOKEN"):
    """Return source implementing exactly the operations in ``selected``."""
    base_url = api.get("base_url") or "https://api.example.com"
    schemes = api.get("auth", []) or []
    auth_scheme = next((item for item in schemes if item.get("type") == "http" and item.get("detail", "").lower() == "bearer"), None)
    if auth_scheme is None:
        auth_scheme = next((item for item in schemes if item.get("type") in ("oauth2", "openIdConnect")), None)
    if auth_scheme is None:
        auth_scheme = next((item for item in schemes if item.get("type") == "apiKey" and item.get("in") in ("header", "query")), None)
    if auth_scheme is None:
        auth_mode = "none"
    elif auth_scheme.get("type") in ("http", "oauth2", "openIdConnect"):
        auth_mode = "bearer"
    else:
        auth_mode = "api_key_" + auth_scheme.get("in", "header")
    auth_parameter = (auth_scheme.get("parameter") if auth_scheme and auth_scheme.get("type") == "apiKey" else "") or ""
    lines = [
        '"""Generated from selected API operations; Python 3.8 compatible."""',
        "import json", "import logging", "import os", "import time", "from datetime import datetime", "from urllib.parse import quote, urlencode", "import requests", "",
        "logger = logging.getLogger(__name__)",
        "BASE_URL = {!r}".format(base_url.rstrip("/")),
        "TOKEN_ENV = {!r}".format(auth_env), "",
        "AUTH_MODE = {!r}".format(auth_mode),
        "AUTH_PARAMETER = {!r}".format(auth_parameter), "",
        "class APIError(RuntimeError):",
        "    def __init__(self, status_code, message, response=None):",
        "        RuntimeError.__init__(self, message)",
        "        self.status_code = status_code",
        "        self.response = response", "",
        "class APIClient:",
        "    def __init__(self, base_url=BASE_URL, token=None, timeout=30):",
        "        self.base_url = base_url.rstrip('/')",
        "        self.token = token if token is not None else os.getenv(TOKEN_ENV)",
        "        self.timeout = timeout", "        self.session = requests.Session()",
        "        self.session.headers.update({'Accept': 'application/json'})",
        "        if self.token and AUTH_MODE == 'bearer':",
        "            self.session.headers.update({'Authorization': 'Bearer ' + self.token})",
        "        elif self.token and AUTH_MODE == 'api_key_header' and AUTH_PARAMETER:",
        "            self.session.headers.update({AUTH_PARAMETER: self.token})", "",
        "    def _request(self, method, path, params=None, json_body=None):",
        "        params = dict(params or {})",
        "        if self.token and AUTH_MODE == 'api_key_query' and AUTH_PARAMETER:",
        "            params[AUTH_PARAMETER] = self.token",
        "        for attempt in range(4):",
        "            try:",
        "                response = self.session.request(method, self.base_url + path, params=params, json=json_body, timeout=self.timeout)",
        "                logger.info('API request completed: %s %s (HTTP %s)', method, path, response.status_code)",
        "            except requests.RequestException as exc:",
        "                if attempt < 3:",
        "                    time.sleep(min(2 ** attempt, 8))",
        "                    continue",
        "                logger.error('API network request failed: %s %s', method, path)",
        "                raise APIError(None, 'Network request failed: {}'.format(exc))",
        "            if response.status_code == 429 or 500 <= response.status_code <= 599:",
        "                if attempt < 3:",
        "                    retry_after = response.headers.get('Retry-After', '')",
        "                    try:",
        "                        delay = min(max(float(retry_after), 0), 30) if retry_after else min(2 ** attempt, 8)",
        "                    except (TypeError, ValueError):",
        "                        delay = min(2 ** attempt, 8)",
        "                    time.sleep(delay)",
        "                    continue",
        "            if response.status_code >= 400:",
        "                try:",
        "                    detail = response.json()",
        "                except ValueError:",
        "                    detail = response.text[:1000]",
        "                raise APIError(response.status_code, 'API returned HTTP {}: {}'.format(response.status_code, detail), detail)",
        "            if response.status_code == 204:",
        "                return None",
        "            try:",
        "                return response.json()",
        "            except ValueError:",
        "                return response.text", "",
    ]
    for ep in selected:
        params = ep.get("parameters", [])
        path_params = [p.get("name") for p in params if p.get("in") == "path" and p.get("name")]
        query_params = [p.get("name") for p in params if p.get("in") == "query" and p.get("name")]
        arguments = ["self"] + ["path_params=None"] if path_params else ["self"]
        arguments += ["params=None", "json_body=None"]
        fn = operation_method_name(ep.get("operation_id"), ep.get("method", "GET"), ep.get("path", "/operation"))
        lines.append("    def {}({}):".format(fn, ", ".join(arguments)))
        lines.append("        \"\"\"{}\"\"\"".format((ep.get("summary") or ep.get("method", "GET") + " " + ep.get("path", "")).replace('"', "'")))
        lines.append("        path_params = path_params or {}" if path_params else "        path_params = {}")
        lines.append("        path = {!r}".format(ep.get("path", "/")))
        if path_params:
            lines.append("        for key in {}:".format(repr(path_params)))
            lines.append("            if key not in path_params:")
            lines.append("                raise ValueError('Missing path parameter: {}'.format(key))")
            lines.append("            path = path.replace('{' + key + '}', quote(str(path_params[key]), safe=''))")
        if query_params:
            lines.append("        query = {{key: value for key, value in (params or {{}}).items() if key in {}}}".format(repr(query_params)))
        else:
            lines.append("        query = params or {}")
        lines.append("        return self._request({!r}, path, params=query, json_body=json_body)".format(ep.get("method", "GET").upper()))
        lines.append("")
    lines.extend(["if __name__ == '__main__':", "    print('Generated client ready. Set the token environment variable and call a selected method.')", ""])
    return "\n".join(lines)


def merge_ai_operation_methods(base_source, generated_source, selected):
    """Merge only selected AI methods into deterministic client infrastructure."""
    generated_tree = ast.parse(generated_source)
    base_tree = ast.parse(base_source)

    def get_methods(tree):
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "APIClient":
                return dict((child.name, child) for child in node.body if isinstance(child, ast.FunctionDef))
        return {}

    generated_methods = get_methods(generated_tree)
    base_methods = get_methods(base_tree)
    names = [operation_method_name(ep.get("operation_id"), ep.get("method", "GET"), ep.get("path", "/operation")) for ep in selected]
    missing = [name for name in names if name not in generated_methods]
    if missing:
        raise ValueError("AI output is missing selected operation method(s): {}".format(", ".join(missing)))
    missing_base = [name for name in names if name not in base_methods]
    if missing_base:
        raise ValueError("Deterministic scaffold is missing selected operation method(s): {}".format(", ".join(missing_base)))

    base_lines = base_source.splitlines()
    generated_lines = generated_source.splitlines()
    replacements = []
    for name in names:
        base_node = base_methods[name]
        ai_node = generated_methods[name]
        method_lines = generated_lines[ai_node.lineno - 1:ai_node.end_lineno]
        replacements.append((base_node.lineno - 1, base_node.end_lineno, method_lines))
    for start, end, method_lines in sorted(replacements, key=lambda item: item[0], reverse=True):
        base_lines[start:end] = method_lines
    return "\n".join(base_lines) + "\n"
