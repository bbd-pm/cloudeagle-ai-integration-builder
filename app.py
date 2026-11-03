import json
import re
import ast
import hashlib
import ipaddress
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, unquote
from typing import Any, Tuple

import requests
import streamlit as st
import yaml
import pandas as pd

from calendly_connector import CalendlyAPIError, CalendlyClient
from connector_generator import generate_python as generate_selected_python, merge_ai_operation_methods
from ai_generation import build_generation_prompt, build_operation_manifest
from connector_validator import validate_generated_code
from groq_provider import GroqProvider, GroqProviderError
from generation_state import invalidate_stale_generated_code
from operation_selection import default_selected_operation_ids, endpoint_category, groq_suggest_operation_ids, merge_visible_selection, operation_remark, operation_selection_key, selected_operations

st.set_page_config(page_title="CloudEagle · AI Integration Builder", page_icon="🔌", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.25rem; max-width: 1380px; padding-bottom: 3rem;}
[data-testid="stSidebar"] {background:#f7f9fc; border-right:1px solid #e5eaf1;}
h1, h2, h3 {letter-spacing:-0.025em; color:#172b4d;}
h1 {font-size:2rem !important;}
.step-card {background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:13px 16px;min-height:86px;}
.step-card.active {border-color:#1769aa;box-shadow:0 0 0 1px #1769aa inset;}
.step-card.complete {border-color:#9ccbb0;background:#f7fcf8;}
.section-note {color:#64748b;font-size:0.92rem;}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius:12px !important; border-color:#e2e8f0 !important;}
div.stButton > button[kind="primary"] {background:#155eef;border-color:#155eef;border-radius:8px;font-weight:600;min-height:2.65rem;}
div.stButton > button {border-radius:8px;}
[data-testid="stDataFrame"], [data-testid="stDataEditor"] {border:1px solid #e2e8f0;border-radius:9px;overflow:hidden;}
[data-testid="stMetric"] {background:#f8fafc;border:1px solid #e8edf3;border-radius:10px;padding:12px 14px;}
div[data-testid="stExpander"] {border:1px solid #e2e8f0;border-radius:10px;}
</style>
""", unsafe_allow_html=True)

MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 15
USER_AGENT = "CloudEagle-Documentation-Fetcher/0.2"
MAX_DOC_PAGES = 500


class LinkParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.links = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() in ("a", "link") and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag.lower() == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])


class DocumentationTextParser(HTMLParser):
    """Collect readable HTML text while ignoring scripts and styles."""
    def __init__(self):
        super().__init__()
        self.parts = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style", "noscript", "svg"):
            self._ignored += 1
        elif tag.lower() in ("p", "br", "div", "li", "pre", "code", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg") and self._ignored:
            self._ignored -= 1
        elif tag.lower() in ("p", "div", "li", "pre", "code", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored:
            self.parts.append(data)


def validate_public_url(url):
    """Basic SSRF guard for a prototype: allow public HTTP(S) hosts only."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// documentation URLs are supported.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a valid public URL without embedded credentials.")
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports (80 and 443) are allowed.")

    host = parsed.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local and private network URLs are blocked.")

    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("Private, loopback and reserved IP addresses are blocked.")
    except ValueError as exc:
        # Re-raise our explicit non-global address error; otherwise resolve DNS.
        if "blocked" in str(exc):
            raise
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror:
            raise ValueError("The documentation hostname could not be resolved.")
        for item in addresses:
            resolved = ipaddress.ip_address(item[4][0].split("%")[0])
            if not resolved.is_global:
                raise ValueError("The hostname resolves to a private or reserved IP address, so it was blocked.")
    return url


def safe_get(url):
    """Fetch with bounded size, timeout and manually validated redirects."""
    current = validate_public_url(url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json,application/yaml,text/yaml,*/*"})
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(current, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("The server returned a redirect without a Location header.")
            current = validate_public_url(urljoin(current, location))
            continue
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_DOC_BYTES:
            response.close()
            raise ValueError("The documentation response exceeds the 5 MB prototype limit.")
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                total += len(chunk)
                if total > MAX_DOC_BYTES:
                    response.close()
                    raise ValueError("The documentation response exceeds the 5 MB prototype limit.")
                chunks.append(chunk)
        content = b"".join(chunks)
        content_type = response.headers.get("Content-Type", "").lower()
        final_url = response.url
        response.close()
        return content, content_type, final_url
    raise ValueError("Too many redirects while fetching documentation.")


def looks_like_spec(raw, content_type="", url=""):
    lower_url = url.lower().split("?")[0]
    return (lower_url.endswith((".json", ".yaml", ".yml")) or
            "json" in content_type or "yaml" in content_type or
            '"openapi"' in raw[:2000].lower() or
            '"swagger"' in raw[:2000].lower() or
            re.search(r"(?m)^\s*(openapi|swagger)\s*:", raw[:2000]) is not None)


def load_spec(raw):
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("The document must contain an OpenAPI object.")
    if "openapi" not in data and "swagger" not in data:
        raise ValueError("No OpenAPI/Swagger version was found. Please provide an OpenAPI 3.x or Swagger 2.x specification.")
    if not isinstance(data.get("paths", {}), dict):
        raise ValueError("The OpenAPI document has an invalid 'paths' section.")
    return data


def extract_documented_operations(raw, source_url):
    """Extract only explicitly printed HTTP method/path pairs from a docs page."""
    parser = DocumentationTextParser()
    parser.feed(raw)
    doc_text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    endpoint_pattern = re.compile(
        r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9._~!$&'()*+,;=:@%{}<>/?#-]*)",
        re.IGNORECASE,
    )
    paths = {}
    heading = re.search(r"(?:^|\s)([A-Z][^\n]{1,120}?)(?:\s+)(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/", doc_text)
    page_summary = heading.group(1).strip(" :-") if heading else ""
    for match in endpoint_pattern.finditer(doc_text):
        method = match.group(1).lower()
        path = match.group(2).rstrip(".,;)")
        if not path.startswith("/") or len(path) > 300:
            continue
        preceding = doc_text[max(0, match.start() - 180):match.start()].strip()
        summary = re.split(r"[.!?]\s+", preceding)[-1][-180:].strip(" :-")
        if not summary or summary.lower() in ("get", "post", "put", "patch", "delete"):
            summary = page_summary
        operation = {"summary": summary or "Extracted from developer documentation", "x-source-url": source_url}
        paths.setdefault(path, {})[method] = operation
    return paths


def discover_spec_from_url(start_url):
    """Try a direct spec URL, linked OpenAPI files, and common Swagger UI config."""
    content, content_type, final_url = safe_get(start_url)
    raw = content.decode("utf-8-sig", errors="replace")
    if looks_like_spec(raw, content_type, final_url):
        return load_spec(raw), final_url, "Direct OpenAPI/Swagger specification"

    parser = LinkParser()
    parser.feed(raw)
    candidates = []
    for href in parser.links:
        absolute = urljoin(final_url, href)
        if urlparse(absolute).path.lower().endswith((".json", ".yaml", ".yml")):
            candidates.append(absolute)

    # Some modern documentation portals publish an llms.txt index that links
    # their raw OpenAPI document even when the HTML page does not link it.
    parsed_page = urlparse(final_url)
    index_url = "{}://{}/llms.txt".format(parsed_page.scheme, parsed_page.netloc)
    index_doc_links = []
    try:
        index_bytes, _, index_final = safe_get(index_url)
        index_text = index_bytes.decode("utf-8-sig", errors="replace")
        for match in re.findall(
            r"https?://[^\s)<>\]]+\.(?:json|ya?ml)(?:\?[^\s)<>\]]*)?",
            index_text,
            re.IGNORECASE,
        ):
            candidate = match.rstrip(".,;")
            # Keep index-based discovery within the documentation site's origin.
            if urlparse(candidate).netloc.lower() == urlparse(index_final).netloc.lower():
                candidates.append(candidate)
        for href in re.findall(r"\]\(([^)]+)\)", index_text):
            candidate = urljoin(index_final, href.strip())
            if urlparse(candidate).netloc.lower() == urlparse(index_final).netloc.lower():
                index_doc_links.append(candidate)
    except Exception:
        pass

    try:
        page_index_bytes, _, page_index_final = safe_get(final_url.rstrip("/") + "/llms.txt")
        page_index_text = page_index_bytes.decode("utf-8-sig", errors="replace")
        for href in re.findall(r"\]\(([^)]+)\)", page_index_text):
            candidate = urljoin(page_index_final, href.strip())
            if urlparse(candidate).netloc.lower() == urlparse(page_index_final).netloc.lower():
                index_doc_links.append(candidate)
    except Exception:
        pass

    # Swagger UI commonly loads swagger-initializer.js, which declares url or urls.
    script_urls = [urljoin(final_url, src) for src in parser.scripts]
    script_urls += [urljoin(final_url, "swagger-initializer.js")]
    for script_url in script_urls[:8]:
        try:
            script_bytes, _, script_final = safe_get(script_url)
            script = script_bytes.decode("utf-8", errors="replace")
            for match in re.findall(r"""(?:url|configUrl)\s*:\s*['"]([^'"]+)['"]""", script):
                candidates.append(urljoin(script_final, match))
            for match in re.findall(r"""['"]((?:https?://|/|\.{1,2}/)[^'"]+\.(?:json|ya?ml)(?:\?[^'"]*)?)['"]""", script, re.I):
                candidates.append(urljoin(script_final, match))
        except Exception:
            pass

    # Some Swagger UI pages expose the specification URL in their HTML.
    for match in re.findall(r"""(?:url|configUrl)\s*[:=]\s*['"]([^'"]+)['"]""", raw):
        candidates.append(urljoin(final_url, match))

    seen = set()
    errors = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        spec_raw = ""
        spec_type = ""
        spec_final = candidate
        try:
            spec_bytes, spec_type, spec_final = safe_get(candidate)
            spec_raw = spec_bytes.decode("utf-8-sig", errors="replace")
            spec = load_spec(spec_raw)
            return spec, spec_final, "OpenAPI/Swagger specification discovered from documentation page"
        except Exception as exc:
            errors.append(str(exc))
            # An advertised .json/.yaml URL may itself be a directory page that
            # lists one or more versioned/spec-specific documents (as Calendly does).
            if len(seen) < 30 and spec_raw:
                if "html" in spec_type or "<html" in spec_raw[:500].lower():
                    index_parser = LinkParser()
                    index_parser.feed(spec_raw)
                    index_host = urlparse(spec_final).netloc.lower()
                    for href in index_parser.links:
                        nested = urljoin(spec_final, href)
                        nested_parsed = urlparse(nested)
                        if (nested_parsed.netloc.lower() == index_host and
                                nested_parsed.path.lower().endswith((".json", ".yaml", ".yml")) and
                                nested not in seen and nested not in candidates):
                            candidates.append(nested)

    # API documentation portals often expose operations as ordinary linked HTML
    # pages instead of publishing an OpenAPI file. Crawl a bounded set of pages
    # on the supplied documentation origin and extract only explicit HTTP routes.
    # Restrict links to the original docs path subtree to avoid crawling the site.
    doc_paths = {}
    origin = (parsed_page.scheme.lower(), parsed_page.netloc.lower())
    root_path = parsed_page.path.rstrip("/")
    # Documentation portals commonly place the landing page and operation
    # pages in sibling sections beneath a shared /api-docs/ directory.
    docs_marker = "/api-docs/"
    if docs_marker in root_path.lower():
        root_path = root_path[:root_path.lower().index(docs_marker) + len(docs_marker) - 1]
    elif "/" in root_path.strip("/"):
        root_path = root_path.rsplit("/", 1)[0]
    queue = [final_url]
    for doc_link in index_doc_links:
        parsed_link = urlparse(doc_link)
        if ((parsed_link.scheme.lower(), parsed_link.netloc.lower()) == origin and
                parsed_link.path.startswith(root_path + "/") and doc_link not in queue):
            queue.append(doc_link)
    visited = set()
    while queue and len(visited) < MAX_DOC_PAGES:
        page_url = queue.pop(0)
        normalized = page_url.split("#", 1)[0]
        if normalized in visited:
            continue
        visited.add(normalized)
        if normalized == final_url:
            page_raw = raw
            page_final = final_url
        else:
            try:
                page_bytes, page_type, page_final = safe_get(normalized)
                page_raw = page_bytes.decode("utf-8-sig", errors="replace")
                is_markdown = urlparse(page_final).path.lower().endswith((".md", ".markdown", ".txt"))
                if "html" not in page_type and "<html" not in page_raw[:500].lower() and not is_markdown:
                    continue
            except Exception:
                continue
        page_parsed = urlparse(page_final)
        if (page_parsed.scheme.lower(), page_parsed.netloc.lower()) != origin:
            continue
        doc_paths.update(extract_documented_operations(page_raw, page_final))
        links = LinkParser()
        links.feed(page_raw)
        page_links = list(links.links)
        page_links.extend(re.findall(r"\]\(([^)]+)\)", page_raw))
        for href in page_links:
            absolute = urljoin(page_final, href).split("#", 1)[0]
            linked = urlparse(absolute)
            if ((linked.scheme.lower(), linked.netloc.lower()) != origin or
                    linked.path.rstrip("/") == root_path or
                    not linked.path.startswith(root_path + "/") or
                    absolute in visited or absolute in queue):
                continue
            if linked.path.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".css", ".js", ".pdf", ".zip")):
                continue
            # Operation pages commonly use verb-led slugs. Prioritize those
            # links so useful endpoint pages are reached within the page cap.
            leaf = linked.path.rstrip("/").rsplit("/", 1)[-1].lower()
            operation_page = re.match(
                r"^(get|list|create|update|delete|remove|cancel|revoke|invite|introspect|revoke|post|put|patch|retrieve|search|read)(-|$)",
                leaf,
            ) is not None
            if operation_page:
                queue.insert(0, absolute)
            else:
                queue.append(absolute)

    if doc_paths:
        title = urlparse(final_url).hostname or "Developer documentation"
        html_spec = {"openapi": "3.0.0", "info": {"title": title, "version": "HTML documentation extraction"},
                     "servers": [{"url": ""}], "paths": doc_paths, "x-source-type": "html-documentation",
                     "x-source-url": final_url}
        if queue:
            html_spec["x-discovery-warning"] = "The linked-page crawl reached its {} page safety limit; some documentation links may remain unvisited.".format(MAX_DOC_PAGES)
        return html_spec, final_url, "HTML developer documentation (linked operations extracted)"

    # Fallback for ordinary HTML developer docs that publish explicit method/path pairs.
    paths = extract_documented_operations(raw, final_url)
    if not paths:
        raise ValueError(
            "No OpenAPI specification or explicit HTTP method/path pairs were found on this page. "
            "The importer crawled linked pages on the documentation site but found no explicit "
            "operations. Upload an OpenAPI JSON/YAML file or provide a page that documents "
            "operations such as GET /v1/users."
        )
    title = urlparse(final_url).hostname or "Developer documentation"
    html_spec = {"openapi": "3.0.0", "info": {"title": title, "version": "HTML documentation extraction"},
                 "servers": [{"url": ""}], "paths": paths, "x-source-type": "html-documentation",
                 "x-source-url": final_url}
    return html_spec, final_url, "HTML developer documentation (explicit operations extracted)"


def resolve_ref(spec, ref):
    if not ref.startswith("#/"):
        return {}
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict):
            return {}
        node = node.get(part)
    return node if isinstance(node, dict) else {}


def expand_schema_refs(spec, value, seen=None, depth=0):
    if seen is None:
        seen = set()
    if depth > 7:
        return value
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            resolved = resolve_ref(spec, ref)
            merged = dict(resolved)
            merged.update(dict((key, item) for key, item in value.items() if key != "$ref"))
            value = merged
            seen = set(seen) | {ref}
        return dict((key, expand_schema_refs(spec, item, seen, depth + 1)) for key, item in value.items())
    if isinstance(value, list):
        return [expand_schema_refs(spec, item, seen, depth + 1) for item in value[:100]]
    return value


def get_parameters(spec, path_item, operation):
    params = []
    for p in (path_item.get("parameters", []) + operation.get("parameters", [])):
        if "$ref" in p:
            p = resolve_ref(spec, p["$ref"])
        if p:
            params.append(p)
    return params


def analyze_spec(spec):
    endpoints = []
    paths = spec.get("paths", {}) or {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            method = method.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} or not isinstance(op, dict):
                continue
            params = get_parameters(spec, item, op)
            param_names = [p.get("name", "") for p in params]
            pagination_hints = [n for n in param_names if any(k in n.lower() for k in ("page", "cursor", "offset", "limit", "count", "token"))]
            responses = op.get("responses", {}) or {}
            success = responses.get("200") or responses.get("201") or next((v for k, v in responses.items() if str(k).startswith("2")), {})
            schema = {}
            try:
                content = success.get("content", {})
                media = content.get("application/json", {})
                schema = media.get("schema", {})
                schema = expand_schema_refs(spec, schema)
            except Exception:
                pass
            endpoints.append({
                "method": method,
                "path": path,
                "operation_id": op.get("operationId") or "{}_{}".format(method.lower(), re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")),
                "summary": op.get("summary") or op.get("description") or "",
                "description": op.get("description") or "",
                "documentation_text": op.get("x-documentation-text") or "",
                "tags": op.get("tags", []),
                "request_body": expand_schema_refs(spec, op.get("requestBody", {})),
                "parameters": params,
                "pagination_hints": pagination_hints,
                "response_schema": schema,
                "responses": list(responses.keys()),
                "source_url": op.get("x-source-url") or spec.get("x-source-url", "")
            })
    security_schemes = (
        spec.get("components", {}).get("securitySchemes", {})
        or spec.get("securityDefinitions", {})
        or {}
    )
    auth = []
    for name, scheme in security_schemes.items():
        auth.append({"name": name, "type": scheme.get("type", "unknown"),
                     "detail": scheme.get("scheme", "") or scheme.get("in", ""),
                     "in": scheme.get("in", ""), "parameter": scheme.get("name", "")})
    servers = spec.get("servers", [])
    base_url = servers[0].get("url", "") if servers else spec.get("host", "")
    return {
        "title": spec.get("info", {}).get("title", "Unnamed API"),
        "version": spec.get("info", {}).get("version", ""),
        "base_url": base_url,
        "endpoints": endpoints,
        "auth": auth,
        "openapi_version": spec.get("openapi", spec.get("swagger", "unknown")),
        "source_type": spec.get("x-source-type", "openapi"),
        "discovery_warning": spec.get("x-discovery-warning", "")
    }


def validate_python(code):
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, "Python syntax error on line {}: {}".format(exc.lineno, exc.msg)
    banned = {"eval", "exec", "compile", "__import__"}
    calls = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in banned:
            calls.append(node.func.id)
    if calls:
        return False, "Potentially unsafe dynamic execution detected: " + ", ".join(sorted(set(calls)))
    return True, "Python syntax check passed. This is not a security certification or proof of runtime correctness."


def run_calendly_live_flow():
    """Use a token for one callback, clear the password widget, and save only results."""
    token = st.session_state.get("calendly_pat", "").strip()
    st.session_state["calendly_pat"] = ""
    max_records = int(st.session_state.get("calendly_max_records", 100))
    if not st.session_state.get("calendly_live_enabled", False):
        st.session_state["calendly_result"] = {
            "status": "Not connected",
            "error": "Select both read-only Calendly operations before running the live test.",
        }
        return
    if not token:
        st.session_state["calendly_result"] = {
            "status": "Not connected",
            "error": "Enter a Calendly Personal Access Token before running the live test.",
        }
        return

    started = time.perf_counter()
    client = None
    user = None
    events = []
    pagination_complete = False
    error = None
    try:
        client = CalendlyClient(token)
        user = client.get_current_user()
        try:
            events, pagination_complete = client.list_scheduled_events(
                user.get("uri"), max_records=max_records
            )
        except CalendlyAPIError as exc:
            error = exc.to_dict()
        except Exception as exc:
            error = {"message": "Event retrieval failed: {}".format(str(exc).replace(token, "[REDACTED]"))}
    except CalendlyAPIError as exc:
        error = exc.to_dict()
    except Exception as exc:
        error = {"message": "Connection failed: {}".format(str(exc).replace(token, "[REDACTED]"))}
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        request_count = client.request_count if client is not None else 0
        status_history = list(client.status_history) if client is not None else []
        # Drop the only long-lived reference to the runtime client and its token.
        client = None
        token = ""

    st.session_state["calendly_result"] = {
        "status": "Connected" if error is None else "Failed",
        "user": user,
        "events": events,
        "pagination_complete": pagination_complete,
        "request_count": request_count,
        "status_history": status_history,
        "duration_ms": duration_ms,
        "error": error,
        "max_records": max_records,
    }


def run_ai_generation(spec, selected, api_info, validation_errors=None, auth_env="API_ACCESS_TOKEN"):
    """Make one bounded Groq attempt and retain only its code and validation result."""
    attempts = int(st.session_state.get("ai_generation_attempts", 0))
    if attempts >= 2:
        st.session_state["ai_generation_error"] = "The two-attempt AI generation limit was reached for this selection."
        return
    st.session_state["ai_generation_attempts"] = attempts + 1
    st.session_state.pop("ai_generation_error", None)
    try:
        prompt = build_generation_prompt(spec, selected, api_info, validation_errors)
        provider = GroqProvider()
        generated = provider.generate_code(prompt)
        deterministic_base = generate_selected_python(api_info, selected, auth_env)
        try:
            assembled = merge_ai_operation_methods(deterministic_base, generated, selected)
        except (SyntaxError, ValueError) as exc:
            validation = {"ok": False, "code": str(generated), "errors": [str(exc)], "message": "AI operation generation did not match the deterministic client scaffold."}
        else:
            validation = validate_generated_code(assembled, selected)
        st.session_state["ai_generated_code"] = validation["code"]
        st.session_state["ai_validation"] = validation
    except GroqProviderError as exc:
        st.session_state.pop("ai_generated_code", None)
        st.session_state.pop("ai_validation", None)
        st.session_state["ai_generation_error"] = exc.message
    except ValueError as exc:
        st.session_state.pop("ai_generated_code", None)
        st.session_state.pop("ai_validation", None)
        st.session_state["ai_generation_error"] = "AI generation input is invalid: {}".format(str(exc)[:1200])
    except Exception as exc:
        st.session_state.pop("ai_generated_code", None)
        st.session_state.pop("ai_validation", None)
        # Avoid rendering provider exception payloads which might contain request data.
        st.session_state["ai_generation_error"] = "AI generation could not complete ({}). Check the selected API metadata and retry.".format(type(exc).__name__)


st.title("CloudEagle Integration Builder")
st.caption("Build a secure SaaS data connector from your API documentation.")

with st.sidebar:
    st.markdown("### Workflow")
    sidebar_api = st.session_state.get("api_analysis")
    sidebar_connected = st.session_state.get("calendly_result", {}).get("status") == "Connected"
    sidebar_generated = bool(st.session_state.get("generated_code") or st.session_state.get("ai_generated_code"))
    sidebar_validated = bool(st.session_state.get("validation", (False,))[0]) or bool((st.session_state.get("ai_validation") or {}).get("ok"))
    for label, done in (("1 · Import documentation", bool(sidebar_api)),
                        ("2 · Select operations", bool(sidebar_api)),
                        ("3 · Generate connector", sidebar_generated),
                        ("4 · Validate and test", sidebar_validated or sidebar_connected)):
        st.markdown("{} **{}**".format("✓" if done else "○", label))
    st.divider()
    groq_status = GroqProvider.configuration_status()
    if groq_status["configured"] and groq_status["sdk_available"]:
        st.caption("AI generation · Ready · {}".format(groq_status["model"]))
    elif not groq_status["sdk_available"]:
        st.caption("AI generation · SDK unavailable")
    else:
        st.caption("AI generation · Not configured")
    st.divider()
    with st.expander("Developer diagnostics", expanded=False):
        if sidebar_api:
            st.write("API:", sidebar_api.get("title", "Unnamed API"))
            st.write("Version:", sidebar_api.get("version") or sidebar_api.get("openapi_version", "Not declared"))
            st.write("Source format:", sidebar_api.get("source_type", "openapi"))
            st.write("Base URL:", sidebar_api.get("base_url") or "Not declared")
            st.write("Documentation source:", st.session_state.get("source_url", "Not recorded"))
            st.write("Discovered operations:", len(sidebar_api.get("endpoints", [])))
        st.caption("AI-generated code is statically checked and is never executed by this application. The Calendly example uses a separate fixed read-only client.")

progress_api = st.session_state.get("api_analysis")
progress_selected_count = 0
if progress_api:
    progress_spec_signature = hashlib.sha256(json.dumps(st.session_state.get("api_spec", {}), sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    progress_selection_key = "selected_operations_{}".format(progress_spec_signature)
    progress_keys = st.session_state.get(progress_selection_key)
    if progress_keys is None:
        progress_defaults = default_selected_operation_ids(progress_api.get("endpoints", []))
        progress_defaults.update(st.session_state.get("ai_default_operation_ids", []))
        progress_keys = set(operation_selection_key(index, endpoint) for index, endpoint in enumerate(progress_api.get("endpoints", [])) if endpoint.get("operation_id") in progress_defaults)
    progress_selected_count = len(selected_operations(progress_api.get("endpoints", []), progress_keys))
progress_generated = bool(st.session_state.get("generated_code") or st.session_state.get("ai_generated_code"))
progress_validated = bool(st.session_state.get("validation", (False,))[0]) or bool((st.session_state.get("ai_validation") or {}).get("ok"))
progress_connected = st.session_state.get("calendly_result", {}).get("status") == "Connected"
progress_steps = [
    ("1", "Import documentation", bool(progress_api), not bool(progress_api)),
    ("2", "Select operations", bool(progress_api) and progress_selected_count > 0, bool(progress_api) and progress_selected_count == 0),
    ("3", "Generate connector", progress_generated, progress_selected_count > 0 and not progress_generated),
    ("4", "Validate and test", progress_validated or progress_connected, progress_generated and not progress_validated),
]
step_columns = st.columns(4)
for step_column, (number, label, complete, active) in zip(step_columns, progress_steps):
    css_class = "complete" if complete else "active" if active else ""
    state_text = "Complete" if complete else "In progress" if active else "Next"
    step_column.markdown(
        '<div class="step-card {}"><span style="color:#64748b;font-size:.8rem">STEP {}</span><br><strong>{}</strong><br><span style="color:#64748b;font-size:.82rem">{}</span></div>'.format(css_class, number, label, state_text),
        unsafe_allow_html=True,
    )

st.markdown("### 1 · Import API documentation")
st.caption("Start with a public developer-docs URL or an OpenAPI/Swagger file. The application discovers documented operations without crawling unrelated sites.")
with st.container(border=True):
    input_mode = st.radio("Choose input method", ["Documentation URL", "Upload OpenAPI file"], horizontal=True)
    
    url_value = ""
    uploaded = None
    if input_mode == "Documentation URL":
        url_value = st.text_input("Public API documentation or OpenAPI URL", placeholder="https://developer.example.com/api-docs")
        st.caption("Supports direct OpenAPI URLs, Swagger UI discovery, same-site llms.txt OpenAPI links, and explicit endpoint extraction from a single HTML docs page.")
    else:
        uploaded = st.file_uploader("Upload OpenAPI JSON or YAML", type=["json", "yaml", "yml"])
    
    analyze = st.button("Analyze API documentation", type="primary", use_container_width=True)

if analyze:
    try:
        source_label = ""
        if input_mode == "Documentation URL":
            if not url_value.strip():
                raise ValueError("Enter a documentation URL first.")
            with st.spinner("Fetching documentation and discovering OpenAPI specification..."):
                spec, resolved_url, source_label = discover_spec_from_url(url_value.strip())
            st.session_state["source_url"] = resolved_url
            st.session_state["source_label"] = source_label
        else:
            if uploaded is None:
                raise ValueError("Upload an OpenAPI JSON or YAML file first.")
            raw = uploaded.getvalue().decode("utf-8-sig")
            spec = load_spec(raw)
            st.session_state["source_url"] = uploaded.name
            st.session_state["source_label"] = "Uploaded OpenAPI/Swagger file"
        st.session_state["api_spec"] = spec
        analyzed_api = analyze_spec(spec)
        st.session_state["api_analysis"] = analyzed_api
        imported_signature = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        for state_key in list(st.session_state.keys()):
            if state_key in (
                "selected_operations_{}".format(imported_signature),
                "capability_filter_{}".format(imported_signature),
                "operation_search_{}".format(imported_signature),
                "operation_page_{}".format(imported_signature),
            ) or state_key.startswith("operation_checkbox_{}_".format(imported_signature)):
                st.session_state.pop(state_key, None)
        automatic_defaults = default_selected_operation_ids(analyzed_api["endpoints"])
        ai_defaults = set()
        if not automatic_defaults and groq_status["configured"] and groq_status["sdk_available"]:
            try:
                ai_defaults = groq_suggest_operation_ids(spec, analyzed_api["endpoints"], analyzed_api, GroqProvider())
            except Exception:
                # Classification is an optional fallback; documentation import itself remains usable.
                st.session_state["ai_default_selection_warning"] = "AI could not classify default operations; choose from the full endpoint list."
            else:
                st.session_state.pop("ai_default_selection_warning", None)
        st.session_state["ai_default_operation_ids"] = list(ai_defaults)
        st.session_state.pop("generated_code", None)
        st.session_state.pop("generated_type", None)
        st.session_state.pop("generated_selection_signature", None)
        st.session_state.pop("calendly_result", None)
        st.session_state.pop("ai_generated_code", None)
        st.session_state.pop("ai_validation", None)
        st.session_state.pop("ai_generation_error", None)
        st.session_state.pop("ai_generation_attempts", None)
        st.session_state.pop("ai_selection_signature", None)
        st.success("API documentation analyzed successfully.")
    except Exception as exc:
        st.error(str(exc))

api = st.session_state.get("api_analysis")
if api:
    st.divider()
    st.markdown("### 2 · Review and select API operations")
    if api.get("source_type") == "html-documentation":
        st.warning("Operations were extracted from explicit method/path text on linked documentation pages. Review references; response schemas, authentication, and pagination may be incomplete.")
    if api.get("discovery_warning"):
        st.warning(api["discovery_warning"])
    m1, m2, m3 = st.columns(3)
    m1.metric("Endpoints discovered", len(api["endpoints"]))
    m2.metric("Auth schemes", len(api["auth"]))
    m3.metric("Pagination hints", sum(len(e["pagination_hints"]) for e in api["endpoints"]))
    st.markdown("#### {}".format(api["title"]))
    st.caption("Recommended rows match user, license, activity, or usage data. All discovered operations remain available for selection.")
    calendly_api = "calendly" in str(api.get("title", "")).lower() or "calendly" in str(api.get("base_url", "")).lower()
    with st.expander("Technical details", expanded=False):
        spec_label = "HTML documentation extraction" if api.get("source_type") == "html-documentation" else api["openapi_version"]
        st.write("Base URL:", api["base_url"] or "Not declared")
        st.write("Source format:", spec_label)
        st.write("Documentation source:", st.session_state.get("source_url", "Not recorded"))
        if api["auth"]:
            st.markdown("**Authentication schemes**")
            st.dataframe(api["auth"], use_container_width=True, hide_index=True)
        else:
            st.warning("No authentication scheme was declared. This does not prove the API is unauthenticated.")
    selectable_endpoints = api["endpoints"]
    default_operation_ids = default_selected_operation_ids(selectable_endpoints)
    default_operation_ids.update(st.session_state.get("ai_default_operation_ids", []))
    spec_signature = hashlib.sha256(json.dumps(st.session_state.get("api_spec", {}), sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    selection_state_key = "selected_operations_{}".format(spec_signature)
    if selection_state_key not in st.session_state:
        st.session_state[selection_state_key] = set(
            operation_selection_key(index, endpoint)
            for index, endpoint in enumerate(selectable_endpoints)
            if endpoint.get("operation_id") in default_operation_ids
        )
    if st.session_state.get("ai_default_selection_warning"):
        st.info(st.session_state["ai_default_selection_warning"])
    elif st.session_state.get("ai_default_operation_ids"):
        st.caption("AI suggested default selections because operation metadata did not identify obvious capability matches.")
    filter_col, search_col = st.columns([1, 2])
    capability_filter = filter_col.selectbox("Capability", ["All", "Users", "Licenses", "Usage", "Activity", "Other"], key="capability_filter_{}".format(spec_signature))
    operation_search = search_col.text_input("Search operations", placeholder="Endpoint, operation name, or remark", key="operation_search_{}".format(spec_signature))
    all_operation_keys = set(operation_selection_key(index, endpoint) for index, endpoint in enumerate(selectable_endpoints))
    select_all_col, recommended_col, clear_all_col, selection_help_col = st.columns([1, 1.2, 1, 2.8])
    editor_filter_signature = hashlib.sha256((capability_filter + "|" + operation_search.strip().lower()).encode("utf-8")).hexdigest()[:12]
    editor_key = "operation_table_{}_{}".format(spec_signature, editor_filter_signature)
    if select_all_col.button("Select all operations", key="select_all_operations_{}".format(spec_signature)):
        st.session_state[selection_state_key] = set(all_operation_keys)
        st.session_state.pop(editor_key, None)
    recommended_keys = set(
        operation_selection_key(index, endpoint)
        for index, endpoint in enumerate(selectable_endpoints)
        if endpoint.get("operation_id") in default_operation_ids
    )
    if recommended_col.button("Select recommended", key="select_recommended_operations_{}".format(spec_signature)):
        st.session_state[selection_state_key] = set(recommended_keys)
        st.session_state.pop(editor_key, None)
    if clear_all_col.button("Deselect all", key="deselect_all_operations_{}".format(spec_signature)):
        st.session_state[selection_state_key] = set()
        st.session_state.pop(editor_key, None)
    selection_help_col.caption("Selections apply across every filter. Recommended operations start selected.")
    if calendly_api:
        calendly_target_paths = {("GET", "/users/me"), ("GET", "/scheduled_events")}
        discovered_demo_paths = set((ep["method"], ep["path"]) for ep in api["endpoints"])
        missing_demo_paths = calendly_target_paths - discovered_demo_paths
        st.caption("All discovered operations are available for connector generation. The separate Calendly live demo requires its two read-only GET operations to be selected.")
        if missing_demo_paths:
            st.warning("The supplied specification is missing required demo operation(s): {}".format(", ".join("{} {}".format(method, path) for method, path in sorted(missing_demo_paths))))
    normalized_search = operation_search.strip().lower()
    filtered_endpoints = []
    for index, endpoint in enumerate(selectable_endpoints):
        category = endpoint_category(endpoint)
        searchable = " ".join([endpoint.get("path", ""), endpoint.get("operation_id", ""), endpoint.get("summary", ""), endpoint.get("description", ""), operation_remark(endpoint)]).lower()
        if capability_filter != "All" and category != capability_filter:
            continue
        if normalized_search and normalized_search not in searchable:
            continue
        filtered_endpoints.append((index, endpoint))
    selected_keys = set(st.session_state[selection_state_key])
    records = []
    filtered_operation_keys = []
    for index, ep in filtered_endpoints:
        operation_key = operation_selection_key(index, ep)
        category_text = endpoint_category(ep)
        if ep.get("operation_id") in default_operation_ids:
            category_text += " · Recommended"
        reference = ep.get("source_url") or st.session_state.get("source_url", "")
        records.append({
            "Selected": operation_key in selected_keys,
            "Method": ep.get("method", ""),
            "Endpoint": ep.get("path", ""),
            "Operation": ep.get("operation_id", ""),
            "Capability": category_text,
            "Remarks": operation_remark(ep),
            "Reference": reference if str(reference).lower().startswith(("http://", "https://")) else "",
        })
        filtered_operation_keys.append(operation_key)
    st.caption("{} operations shown · {} selected · scroll the table to review the complete list".format(len(records), len(selected_keys)))
    if records:
        edited_records = st.data_editor(
            pd.DataFrame(records),
            key=editor_key,
            height=620,
            hide_index=True,
            use_container_width=True,
            column_order=["Selected", "Method", "Endpoint", "Operation", "Capability", "Remarks", "Reference"],
            column_config={
                "Selected": st.column_config.CheckboxColumn("Select", help="Select this API operation"),
                "Method": st.column_config.TextColumn("Method", disabled=True, width="small"),
                "Endpoint": st.column_config.TextColumn("Endpoint", disabled=True, width="medium"),
                "Operation": st.column_config.TextColumn("Operation", disabled=True, width="medium"),
                "Capability": st.column_config.TextColumn("Capability", disabled=True, width="small"),
                "Remarks": st.column_config.TextColumn("Remarks", disabled=True, width="large"),
                "Reference": st.column_config.LinkColumn("Docs", display_text="Open", disabled=True, width="small"),
            },
        )
        for row_index, row in enumerate(edited_records.to_dict("records")):
            operation_key = filtered_operation_keys[row_index]
            if bool(row.get("Selected")):
                selected_keys.add(operation_key)
            else:
                selected_keys.discard(operation_key)
        st.session_state[selection_state_key] = selected_keys
    elif selectable_endpoints:
        st.info("No operations match the current filters. Clear the search or choose another capability. Your selection is preserved.")
    selected = selected_operations(selectable_endpoints, selected_keys)
    st.metric("Selected operations", len(selected))
    selected_demo_paths = set((ep["method"], ep["path"]) for ep in selected)
    calendly_demo_ready = {("GET", "/users/me"), ("GET", "/scheduled_events")}.issubset(selected_demo_paths)
    selection_signature = json.dumps({
        "api": spec_signature,
        "operations": selected,
    }, sort_keys=True, default=str)
    had_generated_connector = "generated_code" in st.session_state
    generated_output_changed = invalidate_stale_generated_code(st.session_state, selection_signature)
    previous_ai_signature = st.session_state.get("ai_selection_signature")
    ai_selection_changed = previous_ai_signature is not None and previous_ai_signature != selection_signature
    if previous_ai_signature != selection_signature:
        if ai_selection_changed:
            st.session_state.pop("calendly_result", None)
        st.session_state["ai_selection_signature"] = selection_signature
        st.session_state["ai_generation_attempts"] = 0
        st.session_state.pop("ai_generated_code", None)
        st.session_state.pop("ai_validation", None)
        st.session_state.pop("ai_generation_error", None)
    if generated_output_changed or ai_selection_changed:
        st.session_state.pop("calendly_result", None)
        if generated_output_changed and had_generated_connector and selected:
            regenerated = generate_selected_python(api, selected, "CALENDLY_ACCESS_TOKEN" if calendly_api else "API_ACCESS_TOKEN")
            regenerated_ok, regenerated_message = validate_python(regenerated)
            st.session_state["generated_code"] = regenerated
            st.session_state["generated_type"] = "calendly" if calendly_api else "generic"
            st.session_state["generated_selection_signature"] = selection_signature
            st.session_state["validation"] = (regenerated_ok, regenerated_message)
            st.info("Selection changed. Connector code was regenerated for the current operations.")
        else:
            st.info("Endpoint selection changed. Previous generated output was cleared; generate again to match the current selection.")
    st.divider()
    st.markdown("### 3 · Generate connector")
    st.caption("Build common client infrastructure deterministically and add API-specific operation behavior with Groq when selected.")
    generation_cols = st.columns(3)
    generation_cols[0].metric("Application", api.get("title") or "API")
    generation_cols[1].metric("Selected operations", len(selected))
    generation_cols[2].metric("AI model", groq_status["model"] if groq_status["configured"] else "Not configured")
    if calendly_api:
        st.caption("The generated connector includes the operations currently selected above. Live Calendly execution remains limited to GET /users/me and GET /scheduled_events.")
        if not calendly_demo_ready:
            st.info("Select GET /users/me and GET /scheduled_events to enable the live Calendly demo. Connector generation follows any non-empty selection.")
    generator_mode = st.radio(
        "Connector generator",
        ["Deterministic generator", "AI-assisted generator (Groq)"],
        horizontal=True,
        key="connector_generator_mode",
    )
    if generator_mode == "Deterministic generator":
        if calendly_api:
            auth_env = "CALENDLY_ACCESS_TOKEN"
        else:
            auth_env = st.text_input("Environment variable for access token", value="API_ACCESS_TOKEN")
        generate_disabled = not bool(selected)
        generate_label = "Generate Python connector"
        if st.button(generate_label, type="primary", disabled=generate_disabled):
            with st.spinner("Generating connector for the selected operations..."):
                code = generate_selected_python(api, selected, auth_env)
            ok, message = validate_python(code)
            st.session_state["generated_code"] = code
            st.session_state["generated_type"] = "calendly" if calendly_api else "generic"
            st.session_state["generated_selection_signature"] = selection_signature
            st.session_state["validation"] = (ok, message)
        if "generated_code" in st.session_state:
            code = st.session_state["generated_code"]
            ok, message = st.session_state["validation"]
            operation_tab, code_tab, validation_tab = st.tabs(["Selected operations", "Connector code", "Validation"])
            with operation_tab:
                st.dataframe([{"Method": ep["method"], "Endpoint": ep["path"], "Operation": ep["operation_id"], "Capability": endpoint_category(ep), "Remarks": operation_remark(ep)} for ep in selected], use_container_width=True, hide_index=True)
            with code_tab:
                st.code(code, language="python")
                if ok:
                    st.download_button("Download connector.py", code, file_name="connector.py", mime="text/x-python")
                st.caption("The connector contains no token. Supply credentials at runtime.")
            with validation_tab:
                if ok:
                    st.success(message)
                else:
                    st.error(message)

    else:
        groq_ready = groq_status["configured"] and groq_status["sdk_available"]
        ai_attempts = int(st.session_state.get("ai_generation_attempts", 0))
        ai_disabled = not selected or not groq_ready or len(selected) > 12 or ai_attempts >= 2
        if not groq_ready:
            st.info("AI generation requires the Groq SDK and GROQ_API_KEY in the process environment. The API key is never shown here.")
        elif len(selected) > 12:
            st.info("Select no more than 12 operations per AI request to keep the prompt bounded.")
        ai_prompt_error = None
        if selected and len(selected) <= 12:
            try:
                preview_manifest = build_operation_manifest(st.session_state.get("api_spec", {}), selected, api)
                build_generation_prompt(st.session_state.get("api_spec", {}), selected, api)
                gaps = list(preview_manifest.get("documentation_gaps", []))
                for operation in preview_manifest.get("selected_operations", []):
                    gaps.extend(["{} {}: {}".format(operation["method"], operation["path"], gap) for gap in operation.get("documentation_gaps", [])])
                if gaps:
                    with st.expander("Documentation gaps AI generation must not guess over", expanded=False):
                        for gap in gaps:
                            st.markdown("- {}".format(gap))
            except ValueError as exc:
                ai_prompt_error = str(exc)
                st.warning("AI generation is unavailable for this selection: {}".format(ai_prompt_error))
        if ai_prompt_error:
            st.caption("Reduce the selection or re-import the matching OpenAPI document, then try again.")
        ai_disabled = ai_disabled or bool(ai_prompt_error)
        if st.button("Generate with AI", type="primary", disabled=ai_disabled):
            with st.spinner("Preparing selected operation metadata and requesting code from Groq..."):
                run_ai_generation(st.session_state.get("api_spec", {}), selected, api, auth_env="CALENDLY_ACCESS_TOKEN" if calendly_api else "API_ACCESS_TOKEN")
        ai_attempts = int(st.session_state.get("ai_generation_attempts", 0))
        ai_validation = st.session_state.get("ai_validation")
        ai_error = st.session_state.get("ai_generation_error")
        if ai_validation is not None and not ai_validation.get("ok") and ai_attempts == 1:
            if st.button("Regenerate once with AI", disabled=not groq_ready):
                with st.spinner("Making the one allowed regeneration attempt..."):
                    run_ai_generation(st.session_state.get("api_spec", {}), selected, api, ai_validation.get("errors", []), "CALENDLY_ACCESS_TOKEN" if calendly_api else "API_ACCESS_TOKEN")
        elif ai_error and ai_attempts == 1:
            if st.button("Retry AI generation once", disabled=not groq_ready):
                with st.spinner("Making the one allowed retry..."):
                    run_ai_generation(st.session_state.get("api_spec", {}), selected, api, auth_env="CALENDLY_ACCESS_TOKEN" if calendly_api else "API_ACCESS_TOKEN")
        elif ai_attempts >= 2 and (ai_error or (ai_validation is not None and not ai_validation.get("ok"))):
            st.warning("The bounded AI retry limit has been reached for this operation selection.")
        if ai_error:
            st.error(ai_error)
        if ai_validation is not None:
            operation_tab, code_tab, validation_tab = st.tabs(["Selected operations", "Connector code", "Validation"])
            with operation_tab:
                st.dataframe([{"Method": ep["method"], "Endpoint": ep["path"], "Operation": ep["operation_id"], "Capability": endpoint_category(ep), "Remarks": operation_remark(ep)} for ep in selected], use_container_width=True, hide_index=True)
            with code_tab:
                st.code(ai_validation.get("code", ""), language="python")
                if ai_validation.get("ok"):
                    st.download_button(
                        "Download validated AI connector",
                        ai_validation["code"],
                        file_name="ai_connector.py",
                        mime="text/x-python",
                    )
            with validation_tab:
                if ai_validation.get("ok"):
                    st.success(ai_validation.get("message", "Static validation passed."))
                else:
                    st.error(ai_validation.get("message", "Static validation failed."))
                    validation_errors = ai_validation.get("errors", [])
                    if validation_errors:
                        st.write("Validation details:")
                        for validation_error in validation_errors:
                            st.error(validation_error)
                    else:
                        st.error("The validator rejected the source but did not return a specific diagnostic. Regenerate once or inspect the generated source for syntax, selected-method, import, credential, and HTTP-verb issues.")
                st.caption("Static checks are not a security sandbox. The app never executes Groq-generated source code.")

    if calendly_api:
        validation = st.session_state.get("validation", (False, "Not generated"))
        deterministic_calendly_validated = (
            st.session_state.get("generated_type") == "calendly" and bool(validation[0])
        )
        ai_validation = st.session_state.get("ai_validation") or {}
        calendly_connector_validated = deterministic_calendly_validated or bool(ai_validation.get("ok"))
        # The live runner uses its own fixed, read-only Calendly client. It does not execute either generated artifact.
        st.session_state["calendly_live_enabled"] = calendly_demo_ready
        st.divider()
        st.markdown("### 4 · Example integration: Calendly")
        st.caption("This optional read-only example is separate from the generic connector workflow.")
        if not calendly_connector_validated:
            st.info("The live test uses the built-in read-only Calendly client; generated connector validation is shown separately.")
        token_value = st.text_input(
            "Calendly Personal Access Token",
            type="password",
            key="calendly_pat",
            help="Used for this live run only. It is cleared after submission and is never written to disk or connector.py.",
        )
        max_records = st.number_input(
            "Maximum event records for this demo",
            min_value=1,
            max_value=10000,
            value=100,
            step=100,
            key="calendly_max_records",
        )
        st.caption("Requests are read-only. Events are fetched in pages of 100 using Calendly's next_page_token.")
        can_test_calendly = calendly_demo_ready and bool(token_value.strip())
        if not calendly_demo_ready:
            st.caption("Live test is waiting for both GET /users/me and GET /scheduled_events to be selected.")
        if not token_value.strip():
            st.caption("Enter a Calendly Personal Access Token to enable the live test.")
        st.button(
            "Test connection and fetch events",
            type="primary",
            disabled=not can_test_calendly,
            on_click=run_calendly_live_flow,
        )
        result = st.session_state.get("calendly_result", {"status": "Not connected"})
        status = result.get("status", "Not connected")
        if status == "Connected":
            st.success("Integration status: Connected. Calendly authentication and event retrieval succeeded.")
        elif status == "Failed":
            result_error = result.get("error") or {}
            st.error("Integration status: Failed. {}".format(result_error.get("message", "The live run did not complete.")))
            if result_error:
                with st.expander("Structured error details"):
                    st.json(result_error)
        else:
            st.info("Integration status: Not connected. Enter a Personal Access Token to enable live execution.")

        user_status_entries = [item for item in result.get("status_history", []) if item.get("path") == "/users/me"]
        if user_status_entries:
            user_status = user_status_entries[-1].get("status_code")
            st.caption("Connection test GET /users/me: HTTP {}".format(user_status if user_status is not None else "network error"))
        user = result.get("user")
        if user:
            st.markdown("#### Authenticated Calendly user")
            st.dataframe([{
                "Name": user.get("name", "Not returned"),
                "Email": user.get("email", "Not returned"),
                "Scheduling URL": user.get("scheduling_url", "Not returned"),
                "User URI": user.get("uri", "Not returned"),
                "Organization URI": user.get("current_organization", "Not returned"),
            }], use_container_width=True, hide_index=True)

        if "events" in result:
            event_statuses = [
                str(item.get("status_code") if item.get("status_code") is not None else "network error")
                for item in result.get("status_history", []) if item.get("path") == "/scheduled_events"
            ]
            if event_statuses:
                st.caption("GET /scheduled_events response status per attempt/page: HTTP {}".format(", HTTP ".join(event_statuses)))
            event_rows = [{
                "Event name": event.get("name", ""),
                "Status": event.get("status", ""),
                "Start time": event.get("start_time", ""),
                "End time": event.get("end_time", ""),
            } for event in result.get("events", [])]
            c1, c2, c3 = st.columns(3)
            c1.metric("Records retrieved", len(event_rows))
            c2.metric("HTTP request attempts", result.get("request_count", 0))
            c3.metric("Execution duration", "{} ms".format(result.get("duration_ms", 0)))
            complete = result.get("pagination_complete", False)
            if complete:
                pagination_status = "Complete"
            elif result.get("status") == "Failed":
                pagination_status = "Incomplete because a request failed"
            else:
                pagination_status = "Record limit reached while more pages remain"
            st.caption("Pagination: {} · Record limit: {}".format(
                pagination_status,
                result.get("max_records", max_records),
            ))
            st.markdown("#### Scheduled events")
            if event_rows:
                st.dataframe(event_rows, use_container_width=True, hide_index=True)
            else:
                st.info("No scheduled events were returned for this Calendly user.")
    else:
        st.divider()
        st.markdown("### 4 · Validate and test connector")
        deterministic_validation = st.session_state.get("validation", (False, "Not generated"))
        ai_validation = st.session_state.get("ai_validation") or {}
        is_generated = bool(st.session_state.get("generated_code") or st.session_state.get("ai_generated_code"))
        is_validated = bool(deterministic_validation[0]) or bool(ai_validation.get("ok"))
        status_cols = st.columns(3)
        status_cols[0].metric("Generation", "Complete" if is_generated else "Not generated")
        status_cols[1].metric("Validation", "Passed" if is_validated else "Pending" if is_generated else "Not run")
        status_cols[2].metric("API connectivity", "Not tested")
        if is_generated and not is_validated:
            st.warning(ai_validation.get("message") or deterministic_validation[1])
        st.info("Generic API calls are not executed by this app. Download the validated connector and test it in your controlled integration environment. The Calendly example above uses its own fixed read-only runner.")
else:
    st.warning("No endpoints were found in the provided specification.")


st.divider()
st.caption("CloudEagle · Secure SaaS integration workflow")
