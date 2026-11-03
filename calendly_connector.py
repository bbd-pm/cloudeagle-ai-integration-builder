"""Read-only Calendly client used by the Build 03 demo.

The token is accepted only at runtime. It is never written to this module or
included in generated source. The demo client only permits GET operations.
"""

import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from string import Formatter
from urllib.parse import quote

import requests


BASE_URL = "https://api.calendly.com"
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


def _redact(value, token):
    text = str(value or "")
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


class CalendlyAPIError(Exception):
    """Safe, structured provider or transport error."""

    def __init__(self, status_code, message, code=None, request_id=None, retry_after=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after
        Exception.__init__(self, self.user_message())

    def user_message(self):
        if self.status_code == 401:
            return "Calendly rejected the token (401). Check that the Personal Access Token is valid."
        if self.status_code == 403:
            return "Calendly denied access (403). Confirm the token has users:read and scheduled_events:read permissions and your plan permits these endpoints."
        if self.status_code == 429:
            return "Calendly rate limited the request (429). Wait briefly and retry."
        if self.status_code is not None and self.status_code >= 500:
            return "Calendly returned a provider error (HTTP {}). Retry later. {}".format(self.status_code, self.message)
        if self.status_code is None:
            return "Could not reach Calendly: {}".format(self.message)
        return "Calendly API error (HTTP {}): {}".format(self.status_code, self.message)

    def to_dict(self):
        return {
            "status_code": self.status_code,
            "code": self.code,
            "message": self.user_message(),
            "request_id": self.request_id,
            "retry_after": self.retry_after,
        }


class CalendlyClient(object):
    """Small requests-based client with bounded retries and GET-only demo mode."""

    def __init__(self, token, session=None, timeout=20, max_retries=3,
                 base_delay=0.5, max_delay=8.0, sleeper=None, allowed_methods=None):
        if not token or not str(token).strip():
            raise ValueError("A Calendly Personal Access Token is required.")
        self._token = str(token).strip()
        self._session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self._sleep = sleeper or time.sleep
        self.allowed_methods = set(allowed_methods or ("GET",))
        self.request_count = 0
        self.status_history = []
        self._session.headers.update({
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
            "User-Agent": "CloudEagle-Calendly-Integration-Builder/0.3",
        })

    def __repr__(self):
        return "CalendlyClient(token=[REDACTED])"

    def _retry_after_seconds(self, value):
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=None)
                    now = datetime.utcnow()
                else:
                    now = datetime.now(retry_at.tzinfo)
                return max(0.0, (retry_at - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _sleep_before_retry(self, attempt, retry_after=None):
        exponential = min(self.max_delay, self.base_delay * (2 ** attempt))
        delay = exponential if retry_after is None else max(exponential, retry_after)
        self._sleep(min(self.max_delay, delay))

    def _format_path(self, path, path_params):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API paths must be relative paths beginning with one slash.")
        path_params = path_params or {}
        fields = [field for _, field, _, _ in Formatter().parse(path) if field]
        missing = [field for field in fields if field not in path_params]
        if missing:
            raise ValueError("Missing path parameter(s): {}".format(", ".join(missing)))
        safe_values = dict((key, quote(str(value), safe="")) for key, value in path_params.items())
        try:
            rendered = path.format(**safe_values)
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid API path template: {}".format(exc))
        if "{" in rendered or "}" in rendered:
            raise ValueError("The API path contains an unresolved parameter.")
        return rendered

    def request(self, method, path, path_params=None, params=None, json_body=None):
        method = str(method).upper()
        if method not in self.allowed_methods:
            raise ValueError("The Calendly demo is read-only and allows GET requests only.")
        rendered_path = self._format_path(path, path_params)
        url = BASE_URL + rendered_path
        request_kwargs = {"params": params or {}, "timeout": self.timeout, "allow_redirects": False}
        if json_body is not None:
            request_kwargs["json"] = json_body

        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            try:
                response = self._session.request(method, url, **request_kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                self.status_history.append({"method": method, "path": rendered_path, "status_code": None})
                if attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise CalendlyAPIError(
                    None,
                    _redact("Network timeout." if isinstance(exc, requests.Timeout) else "Network connection failed.", self._token),
                    code="network_timeout" if isinstance(exc, requests.Timeout) else "network_error",
                )

            self.status_history.append({
                "method": method,
                "path": rendered_path,
                "status_code": response.status_code,
            })
            if response.status_code in RETRYABLE_STATUSES and attempt < self.max_retries:
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                response.close()
                self._sleep_before_retry(attempt, retry_after)
                continue

            if response.status_code >= 400:
                try:
                    payload = response.json()
                except (ValueError, requests.exceptions.JSONDecodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                errors = payload.get("errors")
                detail = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
                message = detail.get("message") or payload.get("message") or response.reason or "Request failed"
                code = detail.get("code") or payload.get("error")
                request_id = response.headers.get("X-Request-Id") or response.headers.get("X-Correlation-Id")
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                status = response.status_code
                response.close()
                raise CalendlyAPIError(status, _redact(message, self._token), _redact(code, self._token),
                                       request_id, retry_after)

            if 300 <= response.status_code < 400:
                status = response.status_code
                response.close()
                raise CalendlyAPIError(status, "Calendly returned a redirect; it was blocked to keep the PAT on HTTPS.", code="redirect_blocked")

            if response.status_code == 204:
                response.close()
                return None
            try:
                payload = response.json()
            except ValueError:
                response.close()
                raise CalendlyAPIError(response.status_code, "Calendly returned an invalid JSON response.", code="invalid_json")
            response.close()
            return payload

        raise CalendlyAPIError(None, "Request retry limit reached.", code="retry_limit")

    def get_current_user(self):
        payload = self.request("GET", "/users/me")
        resource = payload.get("resource") if isinstance(payload, dict) else None
        if not isinstance(resource, dict):
            raise CalendlyAPIError(None, "Calendly returned an unexpected current-user response.", code="invalid_response")
        return resource

    def list_scheduled_events(self, user_uri, max_records=100):
        if not user_uri:
            raise ValueError("The authenticated Calendly user URI is required.")
        max_records = int(max_records)
        if max_records < 1:
            raise ValueError("Maximum record count must be at least 1.")

        records = []
        cursor = None
        seen_cursors = set()
        page_count = 0
        pagination_complete = False
        max_pages = min(max_records + 1, 100)
        next_cursor = None
        while len(records) < max_records and page_count < max_pages:
            params = {"user": user_uri, "count": 100}
            if cursor:
                params["page_token"] = cursor
            payload = self.request("GET", "/scheduled_events", params=params)
            page_count += 1
            if not isinstance(payload, dict):
                raise CalendlyAPIError(None, "Calendly returned an unexpected scheduled-events response.", code="invalid_response")
            collection = payload.get("collection", [])
            pagination = payload.get("pagination", {}) or {}
            if not isinstance(collection, list) or not isinstance(pagination, dict):
                raise CalendlyAPIError(None, "Calendly returned malformed event pagination data.", code="invalid_response")
            remaining = max_records - len(records)
            records.extend(collection[:remaining])
            next_cursor = pagination.get("next_page_token")
            if not next_cursor:
                pagination_complete = True
                break
            if next_cursor in seen_cursors:
                raise CalendlyAPIError(None, "Calendly repeated a pagination cursor; stopping to avoid a loop.", code="pagination_loop")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if len(records) >= max_records:
            # At a record cap, completion is true only when the last response had no next cursor.
            pagination_complete = not bool(next_cursor)
        return records, pagination_complete


def generate_connector_source(selected_endpoints=None):
    """Return this functional client as downloadable, secret-free connector.py."""
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    if selected_endpoints:
        methods = set(endpoint.get("method", "").upper() for endpoint in selected_endpoints)
        paths = set(endpoint.get("path", "") for endpoint in selected_endpoints)
        if methods != {"GET"} or not {"/users/me", "/scheduled_events"}.issubset(paths):
            raise ValueError("Select GET /users/me and GET /scheduled_events to generate the read-only Calendly connector.")
    return source

