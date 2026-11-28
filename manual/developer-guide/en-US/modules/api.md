# API Adapter

> Code Boundary: `backend/iesplan/api/`; Transport Specification: [Public Contracts](../contracts.md)

`api` is a pure HTTP transport adapter layer translating route parameters, cookie auth context, and request payloads into application use-case commands, and mapping application results or errors into public-contract-compliant HTTP responses. The test for clean routing responsibilities is: **calling the same use case from CLI or a background command requires extracting zero business logic from routes.**

## Responsibilities and Boundaries

- **Responsible for**: FastAPI route definitions, HTTP methods, media-type and payload-size limits; authentication dependency resolution producing `ActorContext`; Pydantic request/response DTO validation; semantic top-level wrapper keys and standard error envelopes; and session/permission-checked controlled streaming downloads.
- **Not responsible for**: Importing or querying ORM models directly, constructing object storage paths, issuing `commit`/`rollback`, chaining multiple application use cases, or returning legacy-plus-new union responses.

## Request Processing Pipeline

```text
HTTP Request
  ↓ Pydantic DTO structural validation (types, required fields, reject undeclared fields)
  ↓ Auth dependency → construct ActorContext (raw credentials never enter business layers)
  ↓ Build command & invoke single application use case
ApplicationResult / AppError
  ↓ Serialize semantic top-level wrapper (OpenAPI / response_model) or standard error envelope
HTTP Response (200 / 201 / 202 / 204 / 4xx / 500)
```

## Development and Verification Standards

- **Unified wrappers and status codes**: Successful JSON responses use semantic top-level keys (`{items, total}` or `{items, next_cursor}` for lists), prohibiting generic `{data, meta}` wrappers, enforced via FastAPI `response_model` schemas and endpoint contract tests; `400` handles domain rule violations, `422` handles structural/schema rejections, and `409` handles business state conflicts such as duplicate names or duplicate artifact versions (full details in [HTTP Semantics](../contracts.md#http-semantics)).
- **Field and precision safety**: Uniform `snake_case`, opaque string IDs, decimal fixed-point strings for exact currency amounts, and strict suppression of internal paths, stack traces, and SQL details.
- **OpenAPI as sole field source**: When adding or modifying endpoints, align the application use case and Pydantic `response_model` first, let the running app emit OpenAPI automatically, and add tests covering permissions, status codes, error envelopes, and critical value round-trips.
