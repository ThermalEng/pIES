# pIES Developer Guide

This guide is intended for project maintainers, extension developers, and contributors, explaining the system architectural boundaries, public data contracts, file format standards, module responsibilities, and deployment/acceptance practices.

Before designing or implementing changes, read the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md) to understand global constraints, then consult the relevant format standard or module manual as needed; module manuals together with each module's public interfaces and tests constitute the authoritative development basis.

## Guide Contents

1. [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md): Global mandatory principles and conflict resolution precedence.
2. [System Architecture Blueprint](architecture.md): System layering, end-to-end business chains, module boundaries, and dependency direction.
3. [File Format Standards](file-formats.md): Specifications for device YAML, data CSV, finance YAML, assembly YAML, calculation configuration, Solver Bundle, and project packages.
4. [Module Development Manual](module-development.md): Overview of backend business module responsibilities and cross-module collaboration rules.
5. [Domain Model and Traceability Chain](domain-model.md): Lifecycle and immutable traceability across drafts, versions, snapshots, tasks, and evidence.
6. [Public Contracts](contracts.md): HTTP/JSON conventions, error envelopes, unit and time baselines, asynchronous tasks, and result reading contracts.
7. [Extension System](extensions.md): Extension mechanisms for device content, equation capabilities, computation providers, and storage adapters.
8. [Models and Algorithms](customization-center.md): Design for user-defined device models, algorithm plugin packages, and shared catalog (`1.1.0` target).
9. [Frontend and Help Center](frontend.md): Frontend layered architecture, state management, GUI-backend contract boundaries, and Help Center mechanism.
10. [Deployment and Operations](deployment.md): Docker containerized deployment, unified configuration, worker concurrency and solve isolation, health diagnostics, and backup/recovery.
11. [Normative Index and Document Boundaries](normative-index.md): Authoritative ownership and maintenance conventions across documents.

## Index by Development Task

| Development Task | Reference Documentation |
|---|---|
| Startup assembly, provider registration, and readiness checks | [Composition Root and Bootstrap](modules/bootstrap.md) |
| Common primitives, diagnostic codes, unit conversion, and time axis | [Core Fundamentals](modules/core.md) |
| Adding built-in devices or maintaining the device descriptor catalog | [Device Catalog](modules/devices.md), [Device Model YAML](formats/device-model-yaml.md) |
| Authoring or validating device/price time-series CSVs | [Device Data CSV](formats/device-data-csv.md), [Price Series CSV](formats/price-series-csv.md) |
| Restricted technical equation parsing and mathematical contribution lowering | [Technical Equation Modeling](modules/modeling.md) |
| Assembly graph validation, port network boundaries, and system equations | [Assembly and Validation](modules/assembly.md), [Assembly YAML](formats/assembly-yaml.md), [Network Boundaries and System Equations](formats/network-relations.md) |
| Finance triplet, named amounts, tax rules, and project evaluation | [Finance Calculation](modules/finance.md), [Finance YAML](formats/finance-yaml.md), [Named Amounts, Payments, and Assets](formats/finance-relations.md) |
| Evaluation scope, frozen baselines, and annual maintenance sources | [Public Contracts](contracts.md#evaluation-scope-and-frozen-baseline), [Annual Maintenance Sources](contracts.md#annual-maintenance-sources) |
| Mathematical problem formulation and Solver Bundle generation | [Computation Generators](modules/generators.md), [Solver Bundle](formats/solver-bundle.md) |
| Isolated execution of solver commands and receipt collection | [Solver Runtime](modules/solver-runtime.md) |
| Full computation pipeline, prediction algorithms, and result adaptation | [Computation Generation and Solving](modules/engines.md) |
| Sensitivity analysis, batch sweeps, and four-dimensional system assessment | [Result Analysis](modules/analysis.md) |
| Immutable object storage, reference lifecycle, and cleanup/reconciliation | [Object Storage](modules/storage.md) |
| Cross-module use-case orchestration and transaction boundaries | [Application Use Cases](modules/application.md) |
| HTTP routing, DTO mapping, and error responses | [API Adapter](modules/api.md) |
| Background daemon, leases, checkpoints, and retries | [Worker](modules/worker.md) |
| Domain table ownership, repositories, and database migrations | [Persistence](modules/persistence.md) |
| Frontend pages, feature layering, browser acceptance, and Help Center | [Frontend and Help Center](frontend.md) |
| Custom model library, algorithm plugins, and shared hub (`1.1.0` target) | [Models and Algorithms](customization-center.md) |

Return to the [Official Documentation Portal](../../README.md) or [Project Entry](../../../README.md).
