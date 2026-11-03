# CloudEagle Integration Builder — Build 03

Python 3.8.10 compatible Streamlit prototype for discovering API operations from developer docs/OpenAPI and generating connector code deterministically or with Groq. The Calendly live path uses a separate, controlled read-only API client; Groq-generated source is never executed by the app.

## Workflow

1. Import a public developer-docs URL or upload OpenAPI/Swagger JSON/YAML.
2. Review the operation table. Operations matching user, license, activity and usage data are selected by default; if metadata matching finds none and Groq is configured, it makes a bounded, sanitized classification fallback. Every discovered operation remains selectable. Remarks come from operation documentation and source links appear separately as **Reference**.
3. Generate deterministic code or choose **Generate with AI**. The current selected operation definitions determine the connector. Changing the selection regenerates existing deterministic output and clears stale AI output.
4. Inspect the result. AI output must pass syntax, import, dangerous-call, credential, and selected-method checks before download.
5. For the Calendly API, select `GET /users/me` and `GET /scheduled_events`, generate and validate the functional client, then test it with a Calendly Personal Access Token.
6. Review the authenticated user, event records, request count, elapsed time, and pagination status.

## Capabilities

- Documentation URL ingestion, Swagger UI discovery, same-site `llms.txt` OpenAPI discovery, and single-page HTML extraction of explicit method/path pairs.
- OpenAPI/Swagger JSON and YAML upload and parsing.
- Endpoint/auth discovery and capability-based defaults across all discovered operations. Deterministic connector infrastructure includes exactly the checked operations; AI-generated operation methods are merged into that deterministic infrastructure. The separate Calendly live demo runner remains limited to its two read-only GET operations.
- Groq-assisted generation using only the selected operations' OpenAPI metadata. Prompt content is size-bounded and examples/credential-like values are redacted.
- Static validation of untrusted AI output. Invalid code cannot be downloaded; one bounded retry is available. Static checks are not a sandbox.
- Calendly live test through a separate runner that only uses GET requests. The generated code is not run in the Streamlit process.
- Calendly event retrieval sends `user=<authenticated user URI>`, `count=100`, and `page_token=<next_page_token>`, with a configurable maximum record count and a page limit.
- The Calendly Personal Access Token uses a password input, is cleared after submission, and is never passed to Groq or written to source/disk/logs.
- **Connected** is shown only after real Calendly authentication and event retrieval both succeed. Without a PAT, live execution remains disabled and the status is **Not connected**.

## Windows setup (Python 3.8.10)

Open PowerShell in this project directory. Use the project's interpreter for every Python command:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

To create the environment if it does not exist, use a Python 3.8 launcher and then continue using the environment executable:

```powershell
py -3.8 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Groq 0.33.0 is pinned and was checked in this project environment with Python 3.8.10. Streamlit stays pinned at the installed 1.40.1 version.

## Configure Groq in PowerShell

The app reads the Groq key only from `GROQ_API_KEY`. Enter it as a secure prompt into the current PowerShell process environment; this avoids putting the key in command history or saving it as a persistent user environment variable:

```powershell
$secureGroqKey = Read-Host -Prompt "Groq API key" -AsSecureString
$groqCredential = New-Object -TypeName System.Net.NetworkCredential -ArgumentList @("", $secureGroqKey)
$env:GROQ_API_KEY = $groqCredential.Password
$env:GROQ_MODEL = "openai/gpt-oss-120b"
```

`GROQ_MODEL` is optional; if it is unset, the app defaults to `openai/gpt-oss-120b`. The Groq configuration indicator shows whether a key is configured and which model will be used, but never displays the key. Launch Streamlit from the same PowerShell window so it inherits the process environment. Close that shell to discard the environment variables.

## Run tests

The mock suite uses no Groq network call and no Calendly credentials:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Run the application

From the same PowerShell window used to configure Groq:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open the local URL printed by Streamlit, usually `http://localhost:8501`.

## Calendly test requirements

The user's PAT needs `users:read` and `scheduled_events:read`. The runner calls `GET https://api.calendly.com/users/me`, displays the authenticated user's name, email, scheduling URL, user URI and organization URI, then retrieves event pages using Calendly's documented `next_page_token` cursor. It shows sanitized event names/status/start/end times and execution status. It handles 401, 403, 429, transient 5xx, network errors and timeouts with bounded retries and an HTTP timeout.

No real Calendly PAT is needed to run the automated tests. The application reports **Connected** only after successful real API requests.

## Architecture

- `app.py`: Streamlit workflow orchestration, existing documentation ingestion/discovery, selection and deterministic generation, AI/deterministic UI, and Calendly results.
- `connector_generator.py`: Python 3.8-compatible deterministic source generation for the currently selected operations, including path/query parameters, JSON request bodies, bearer auth, timeout and bounded transient-error retries.
- `operation_selection.py`: capability-based defaults for user, license, activity and usage operations, plus documentation-derived concise remarks.
- `ai_generation.py`: selected-operation manifest, local reference expansion, credential/example scrubbing and bounded Groq prompt construction.
- `groq_provider.py`: environment-only Groq setup, model selection, timeout and sanitized provider error handling.
- `connector_validator.py`: AST syntax/import/dangerous-operation/credential/method checks for untrusted AI code.
- `generation_state.py`: invalidates deterministic output when the endpoint selection changes, preventing stale code from appearing current.
- `calendly_connector.py`: reusable GET-only demo client, retries, structured API errors, safe path formatting and cursor pagination; also provides secret-free generated connector source.
- `tests/`: mock tests for provider behavior, prompt redaction, generated-code validation and Calendly HTTP behavior.

## Product decisions

- Keep developer-docs URLs and OpenAPI/Swagger files as the primary inputs: enterprise integration teams can start from official docs, while OpenAPI supplies the structured fields needed for connector generation.
- Keep cURL and Postman out of the primary onboarding flow because they describe individual requests or internal testing collections, not a complete enterprise integration handoff.
- Preserve deterministic generation as a predictable fallback; add Groq for assistance, not as the source of truth. OpenAPI metadata constrains the prompt and independent validation constrains the downloadable result.
- Keep the Calendly test runner separate from AI-generated code execution. The prototype makes only the documented read-only Calendly calls and reports gaps instead of claiming an integration is complete without successful API responses.

## Known limitations

- AST checks reduce obvious risk but do not prove generated code is safe. The app does not execute AI-generated source.
- Prompt-injection text embedded in public API docs is treated as untrusted input, but static validation cannot guarantee semantic correctness.
- AI generation is capped at 12 selected operations and a 24,000-character operation manifest; reduce the selection when a prompt exceeds the bound.
- HTML extraction covers only explicit method/path pairs on the supplied page. OpenAPI is needed for reliable parameter and response schemas.
- Calendly calls are local, synchronous demo requests. There is no scheduled sync, stored checkpoint, persistent token vault, production deployment, or multi-source reconciliation.
- No real Calendly API call is made until the user supplies a PAT in the password field. A successful mock test is not evidence that a real account is connected.
