# Object Storage

> Code Boundary: `backend/iesplan/storage/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`storage` uniformly manages immutable large objects—dataset files, calculation bundle assets, execution outputs, result evidence, and export archives—isolating physical filesystem or object-store implementations from business modules and maintaining reference-manifest object lifecycles.

## Responsibilities and Boundaries

- **Owns**: Opaque `ObjectId`, `ObjectHandle`, `ObjectOwner`, and the public `ObjectStore` protocol; attaching, detaching, and protecting owner reference manifests (`object_refs`); orphan object preview and direct cleanup; standard local-filesystem `BlobStore` adapter (supporting zero-copy `os.link` with `EXDEV` copy fallback) and module health status.
- **Does not own**: Semantics of project versions, dataset schemas, or user download permissions; physical storage paths and disk layout remain encapsulated strictly inside adapters.

## Writes, Chunked I/O, and Reference Flow

```text
Receive validated input stream within size bounds
  ↓ Stream bytes in chunks to object path & measure written bytes (unlink partial file on failure)
  ↓ Assign opaque ObjectId & persist metadata record
application creates ObjectOwner reference inside business transaction
```

- **Chunked content protocol**: `put_content` and `get_object_content` for bundles and large files obey the [Bundle Chunked Content Protocol](../formats/solver-bundle.md#chunked-content-and-lifecycle), streaming bounded chunks and returning session-independent content handles alongside recorded `size_bytes`; small-document convenience helpers reuse the exact same underlying path.
- **Reference manifests as sole source of truth**: Database transactions and file writes handle normal failures explicitly; any active `ObjectOwner` reference blocks cleanup, with `object_refs` serving as the sole authority without maintaining redundant `ref_count` cache repair state machines.
- **Worker stage asset reuse**: Generated bundles and execution outputs write under attempt-level owners first; final evidence publication attaches evidence-level owner references to the exact same objects, avoiding redundant large-file copies.

## Orphan Object Cleanup

Administrators inspect or remove unreferenced orphan objects via `POST /api/admin/objects/cleanup`:

1. **Preview (`dry_run=true`)**: Returns candidate objects lacking any active `ObjectRef` along with reclaimable byte counts.
2. **Direct cleanup (`dry_run=false`)**: Deletes files and metadata for objects lacking active `ObjectRef` entries while recording cleanup results and audit events; any active `ObjectRef` prevents deletion.
3. **Health checks**: Health endpoints inspect storage root accessibility and metadata only, never substituting capacity guesses for real write failures or adding special recovery layers for non-standard filesystems or external tampering.

## Storage Adapter Extension and Testing

A new storage adapter needs only byte-level read, write, delete, existence/size inspection, and health checks—never authorization or retention rules—and is injected by `bootstrap` once passing unified `ObjectStore` contract tests.
