"""Static checks for untrusted LLM-generated Python connector source."""

import ast
import re

from ai_generation import operation_method_name


MAX_GENERATED_CODE_CHARACTERS = 60000
ALLOWED_IMPORTS = {
    "requests", "requests.exceptions", "json", "time", "typing", "urllib.parse", "datetime", "logging", "os", "__future__",
}
BANNED_IMPORT_ROOTS = {
    "subprocess", "pathlib", "shutil", "socket", "sys", "importlib",
    "pickle", "marshal", "ctypes", "multiprocessing", "builtins", "tempfile",
}
BANNED_CALL_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
}
BANNED_ATTRIBUTE_CALLS = {
    "open", "read", "read_text", "write", "write_text", "unlink", "remove",
    "rmdir", "mkdir", "system", "popen", "spawn", "run", "Popen", "call",
    "check_call", "check_output", "execv", "execve", "send",
}
SENSITIVE_TARGET_RE = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|refresh_?token|personal_?access_?token|client_?secret|password|secret|credential|authorization|token)$",
    re.IGNORECASE,
)
SECRET_TEXT_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}"),
    re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:gsk_|sk-|gh[pousr]_|pat_)[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
]
HTTP_VERB_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "head": "HEAD", "options": "OPTIONS"}
SAFE_CREDENTIAL_PLACEHOLDERS = {
    "", "none", "null", "your_token", "your_api_key", "api_access_token",
    "access_token", "token", "placeholder", "replace_me", "changeme",
}


def strip_code_fences(code):
    text = str(code or "").strip()
    match = re.match(r"^```(?:python|py)?\s*\n?(.*?)\n?```\s*$", text, re.IGNORECASE | re.DOTALL)
    if match:
        text = match.group(1).strip()
    return text


def _target_names(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, (ast.Tuple, ast.List)):
        names = []
        for item in node.elts:
            names.extend(_target_names(item))
        return names
    return []


def _expected_public_methods(selected):
    return set(
        operation_method_name(
            endpoint.get("operation_id"), endpoint.get("method", "GET"), endpoint.get("path", "/operation")
        )
        for endpoint in selected
    )


def _request_method_from_call(node):
    if not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr.lower()
    if attr in HTTP_VERB_METHODS:
        return HTTP_VERB_METHODS[attr]
    if attr not in ("request", "_request"):
        return None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value.upper()
    for keyword in node.keywords:
        if keyword.arg in ("method", "http_method") and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value.upper()
    return "UNVERIFIED"


def validate_generated_code(code, selected):
    clean_code = strip_code_fences(code)
    errors = []
    if not clean_code:
        return {"ok": False, "code": clean_code, "errors": ["Groq returned no Python code."], "message": "Validation failed."}
    if len(clean_code) > MAX_GENERATED_CODE_CHARACTERS:
        return {"ok": False, "code": clean_code, "errors": ["Generated code exceeds the size limit."], "message": "Validation failed."}
    if "```" in clean_code:
        errors.append("Remove surrounding prose or multiple Markdown code blocks; return one Python module only.")
    allowed_http_methods = set(endpoint.get("method", "GET").upper() for endpoint in selected)
    try:
        tree = ast.parse(clean_code)
    except SyntaxError as exc:
        return {
            "ok": False,
            "code": clean_code,
            "errors": ["Python syntax error on line {}: {}".format(exc.lineno, exc.msg)],
            "message": "Generated code did not pass Python syntax validation.",
        }

    api_client_classes = []
    parent_by_node = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_by_node[child] = parent
    dynamic_helpers = set()
    helper_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_request":
            method = _request_method_from_call(node)
            helper_calls.append(method)
    if helper_calls and all(method in allowed_http_methods for method in helper_calls):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _request_method_from_call(node) == "UNVERIFIED":
                parent = parent_by_node.get(node)
                while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent = parent_by_node.get(parent)
                if parent is not None and parent.name == "_request":
                    dynamic_helpers.add(id(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORT_ROOTS or alias.name not in ALLOWED_IMPORTS:
                    errors.append("Import is not allowed: {}".format(alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in BANNED_IMPORT_ROOTS or module not in ALLOWED_IMPORTS:
                errors.append("Import is not allowed: {}".format(module or "relative import"))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALL_NAMES:
                errors.append("Potentially unsafe call is not allowed: {}".format(node.func.id))
            if isinstance(node.func, ast.Attribute) and node.func.attr in BANNED_ATTRIBUTE_CALLS:
                errors.append("File or process operation is not allowed: {}".format(node.func.attr))
            request_method = _request_method_from_call(node)
            if request_method == "UNVERIFIED":
                if id(node) not in dynamic_helpers:
                    errors.append("Use a statically verifiable HTTP verb for each request.")
            elif request_method and request_method not in allowed_http_methods:
                errors.append("HTTP {} is not allowed by the selected API operations.".format(request_method))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr != "getenv":
            errors.append("Only os.getenv is allowed for runtime credential lookup.")
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                text_value = value.value.strip()
                for target in targets:
                    for target_name in _target_names(target):
                        if SENSITIVE_TARGET_RE.search(target_name) and text_value.lower() not in SAFE_CREDENTIAL_PLACEHOLDERS:
                            errors.append("Hardcoded credential assigned to {} is not allowed.".format(target_name))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "APIClient":
            api_client_classes.append(node)
    if not api_client_classes:
        errors.append("Define a reusable class named APIClient.")
    else:
        public_methods = set()
        for class_node in api_client_classes:
            public_methods.update(
                node.name for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
            )
        expected = _expected_public_methods(selected)
        missing = sorted(expected - public_methods)
        extra = sorted(public_methods - expected)
        if missing:
            errors.append("Missing selected operation method(s): {}".format(", ".join(missing)))
        if extra:
            errors.append("Unselected public API method(s) are not allowed: {}".format(", ".join(extra)))

    for pattern in SECRET_TEXT_PATTERNS:
        if pattern.search(clean_code):
            errors.append("Generated source contains a hardcoded credential-like value.")
            break

    errors = sorted(set(errors))
    return {
        "ok": not errors,
        "code": clean_code,
        "errors": errors,
        "message": "Static validation passed. This does not make generated code safe to execute." if not errors else "Generated code did not pass static validation.",
    }

