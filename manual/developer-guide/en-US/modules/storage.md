# Object Storage

> Code Boundary: `backend/iesplan/storage/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`storage` uniformly manages immutable large objects—dataset files, calculation bundle assets, execution outputs, result evidence, and export archives—isolating physical filesystem or object-store implementations from business modules and maintaining reference-manifest object lifecycles.

## Responsibilities and Boundaries

- **Owns**: Opaque `ObjectId`, `ObjectHandle`, `ObjectOwner`, and the public `ObjectStore` protocol; attaching, detaching, and protecting owner reference manifests (`object_refs`); orphan object preview/cleanup and crash reconciliation; underlying `BlobStore` adapters and module health status.
- **Does not own**: Semantics of project versions, dataset schemas, or user download permissions; physical storage paths and disk layout remain encapsulated strictly inside adapters.

## Writes, Chunked I/O, and Reference Flow

```text
Receive validated input stream within size bounds
  ↓ Stream bytes in chunks into temporary blob & measure written bytes
  ↓ Atomically publish immutable object (assign opaque ObjectId)
  ↓ Idempotently persist metadata record
application creates ObjectOwner reference inside business transaction
```

- **Chunked content protocol**: `put_content` and `get_object_content` for bundles and large files obey the [Bundle Chunked Content Protocol](../formats/solver-bundle.md#chunked-content-and-lifecycle), streaming bounded chunks and returning session-independent content handles alongside recorded `size_bytes`; small-document convenience helpers reuse the exact same underlying path.
- **Cross-store consistency and reference protection**: Database transactions and blob writes do not form a single ACID transaction, so confirmed write interruptions converge idempotently via operation IDs; any active `ObjectOwner` reference blocks deletion, while `ref_count` remains purely a rebuildable cache with reference manifests serving as truth.
- **Worker stage asset reuse**: Generated bundles and execution outputs write under attempt-level owners first; final evidence publication attaches evidence-level owner references to the exact same objects, avoiding redundant large-file copies.

## Orphan Object Cleanup and Crash Reconciliation

Administrators inspect or remove unreferenced orphan objects via `POST /api/admin/objects/cleanup`:

1. **Preview (`dry_run=true`)**: Returns candidate objects lacking any active `ObjectRef` along with reclaimable byte counts.
2. **Direct cleanup (`dry_run=false`)**: Deletes files and metadata for objects that remain unreferenced and satisfy explicit business retention rules while recording cleanup results and audit events; any active `ObjectRef` prevents deletion.
3. **Reconciliation and health checks**: Health and status endpoints read object metadata and volume free space only (opening object bytes only on demand); when write interruptions occur, reconciliation detects orphaned disk files or missing blobs.

## Storage Adapter Extension and Testing

A new storage adapter needs only byte-level read, write, delete, existence/size inspection, and health checks—never authorization or retention rules—and is injected by `bootstrap` once passing unified `ObjectStore` contract, concurrency, and crash-retry tests.
