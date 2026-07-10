# Soon to be removed from here

Check the new repository [thallium-solutions/lams3](https://github.com/thallium-solutions/lams3)


# lams3

<p align="center">
  <img src="assets/logo-full.png" alt="Lammergeier logo" width="420">
</p>

`lams3` is a small Lammergeier package for S3-compatible object storage.
It targets AWS S3-style APIs and S3-compatible providers such as Cloudflare R2,
MinIO, Wasabi, Backblaze B2, and custom gateways.

Canonical repository: [thallium-solutions/lams3](https://github.com/thallium-solutions/lams3).

It is currently mirrored under `third_party/` with its own `lamlib.toml`, tests,
and README so it can be used as a normal external Lam library while the package
is being moved out of this repository.

## What you get

- **Simple text object I/O** with `putText`, `getText`, `exists`, `list`, and
  `delete`.
- **Result-returning APIs** (`tryPutText`, `tryGetText`, `tryUploadFile`,
  `tryCopy`, `tryMove`, `tryDeleteMany`, and more) for `?` and `do/catch`.
- **Real-world object workflows**: file upload/download, copy, move, bulk
  delete, key-only listing, buffers, Go `io.Reader` streams, object metadata,
  and presigned GET/PUT URLs.
- **Environment-based setup** through `S3Config.fromEnv()` and
  `S3Client.fromEnv()`.
- **Direct setup** with `S3Config(...)` or `S3Client.connect(...)` when
  credentials come from your own config loader instead of environment variables.
- **S3-compatible endpoints** with configurable path-style addressing.
- **Public URL helpers** that safely encode object keys while preserving path
  separators.
- **Offline tests** for deterministic helpers and optional live roundtrip tests
  for real buckets.

## Package layout

```text
lams3/
├── __init__.lam              # Public package API
├── lamlib.toml               # Library metadata and Go SDK pins
├── README.md                 # This guide
├── CHANGELOG.md              # Package-specific release notes
├── LICENSE                   # Apache License 2.0 text
├── NOTICE                    # lams3 attribution notices
├── .env.example              # Safe environment template
├── assets/
│   └── logo-full.png         # Lammergeier official logo for README rendering
└── tests/
    ├── offline_*.lam         # No credentials required
    ├── live_roundtrip.lam    # Optional real S3/R2 integration test
    └── run_lams3_tests.py    # Package-specific test runner
```

## Configuration

Copy `.env.example` into your shell or secret manager and fill in real values.
Do not commit credentials.

```bash
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_ENDPOINT="account-id.r2.cloudflarestorage.com"
export S3_PUBLIC_ENDPOINT="bucket.example.com"
export S3_REGION="auto"
export S3_BUCKET="bucket-name"
export S3_USE_PATH_STYLE="true"
```

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `S3_ACCESS_KEY_ID` | Yes | S3/R2 access key. |
| `S3_SECRET_ACCESS_KEY` | Yes | S3/R2 secret key. |
| `S3_ENDPOINT` | Yes | API endpoint without a bucket prefix. |
| `S3_PUBLIC_ENDPOINT` | No | Public CDN/custom-domain endpoint for generated URLs. |
| `S3_REGION` | No | Region, defaults to `auto` when omitted. |
| `S3_BUCKET` | Yes | Bucket used by client operations. |
| `S3_USE_PATH_STYLE` | No | Defaults to `true`; set `false`, `0`, or `no` for virtual-host style. |

## Quick start

```lammergeier
from lams3 import S3
from lams3 import S3Client

func main() {
    if not S3.hasEnv() {
        print("missing S3 configuration")
        return
    }

    s3: S3Client = S3Client.fromEnv()
    key: str = "notes/hello.txt"

    print(s3.putText(key, "hello from Lam", "text/plain"))
    print(s3.exists(key))
    print(s3.getText(key))
    print(s3.publicUrl(key))
    print(s3.delete(key))
}
```

### Without environment variables

Use direct configuration when your app already has a config object, secret
manager, or tenant-specific credentials:

```lammergeier
from lams3 import S3Client

func main() {
    s3: S3Client = S3Client.connect(
        "access-key",
        "secret-key",
        "account-id.r2.cloudflarestorage.com",
        "app-bucket",
        "auto",
        "cdn.example.com",
        true,
    )

    print(s3.publicUrl("avatars/ada.png"))
}
```

If you prefer to pass a config object:

```lammergeier
from lams3 import S3Client, S3Config

func main() {
    cfg: S3Config = S3Config("key", "secret", "endpoint.example.com", "bucket")
    s3: S3Client = S3Client(cfg)
    print(s3.putText("hello.txt", "hello", "text/plain"))
}
```

For error-aware flows, use the `try*` methods with `?` inside `do/catch`:

```lammergeier
from lams3 import S3Client

func main() {
    s3: S3Client = S3Client.fromEnv()

    do {
        s3.tryPutText("reports/today.txt", "ready", "text/plain")?
        body: str = s3.tryGetText("reports/today.txt")?
        url: str = s3.presignedGetUrl("reports/today.txt", 900)
        print(body)
        print(url)
    } catch err {
        print(f"s3 failed: {err}")
    }
}
```

### Files, buffers, streams, and metadata

```lammergeier
from lams3 import S3Client, S3Object
from lamos import Os

go! {
    import "strings"
}

func main() {
    s3: S3Client = S3Client.fromEnv()

    # Strings and files.
    s3.tryPutText("notes/today.txt", "ready", "text/plain").unwrap()
    s3.tryUploadFile("report.pdf", "reports/report.pdf", "application/pdf").unwrap()
    s3.tryDownloadFile("reports/report.pdf", "/tmp/report.pdf").unwrap()

    # Lam buffers use list[int], matching lambytes.Bytes helpers.
    bytes: list[int] = [0, 1, 2, 255]
    s3.tryPutBuffer("raw/blob.bin", bytes).unwrap()
    roundTrip: list[int] = s3.getBuffer("raw/blob.bin")
    print(len(roundTrip))

    # Streams are for Go interop values that implement io.Reader.
    reader: any = go!(strings.NewReader("stream body"))
    s3.tryPutStream("streams/body.txt", reader, "text/plain").unwrap()

    # stat() uses HEAD and does not download the object body.
    obj: S3Object = s3.stat("notes/today.txt")
    print(f"{obj.size} bytes, {obj.contentType}")
}
```

For more complete examples, read the test cases:

- `tests/offline_direct_config.lam` shows direct construction.
- `tests/offline_result_do_catch.lam` shows `Result` and `do/catch`.
- `tests/offline_presign_urls.lam` shows presigned URL generation.
- `tests/live_roundtrip.lam` exercises text, files, buffers, streams, stat,
  copy, move, list, presign, and bulk cleanup against a real bucket.

When compiling directly from this repository, point the compiler at
`third_party` as the external-library root:

```bash
/usr/bin/python3 compiler/lammergeier.py app.lam --extlibs third_party -o app
```

## API reference

### `S3Config`

| Member | Type | Description |
|--------|------|-------------|
| `accessKeyId` | `str` | Credential access key. |
| `secretAccessKey` | `str` | Credential secret key. |
| `endpoint` | `str` | S3-compatible API endpoint. |
| `publicEndpoint` | `str` | Public URL endpoint used by `publicUrl`. |
| `region` | `str` | Region, defaulting to `auto`. |
| `bucket` | `str` | Bucket for object operations. |
| `usePathStyle` | `bool` | Whether SDK requests use path-style addressing. |

| Method | Description |
|--------|-------------|
| `S3Config(accessKeyId="", secretAccessKey="", endpoint="", bucket="", region="auto", publicEndpoint="", usePathStyle=true)` | Builds config directly without environment variables. |
| `S3Config.fromEnv()` | Builds config from `S3_*` environment variables. |
| `cfg.isReady()` | Returns `true` when required credentials, endpoint, region, and bucket are present. |
| `cfg.validate()` | Returns `Result.Ok(true)` or `Result.Err(Error("S3ConfigError", ...))`. |

### `S3Client`

| Method | Description |
|--------|-------------|
| `S3Client.fromEnv()` | Creates a client from environment configuration. |
| `S3Client.connect(accessKeyId, secretAccessKey, endpoint, bucket, region="auto", publicEndpoint="", usePathStyle=true)` | Creates a client directly without env variables. |
| `putText(key, text, contentType="text/plain")` | Uploads text and returns success as `bool`. |
| `tryPutText(key, text, contentType="text/plain")` | Uploads text and returns `Result.Ok(true)` or `S3PutError`. |
| `putBytes(key, data, contentType="application/octet-stream")` | Uploads binary-ish string data and returns success as `bool`. |
| `tryPutBytes(key, data, contentType="application/octet-stream")` | Result-returning sibling of `putBytes`. |
| `putBuffer(key, data, contentType="application/octet-stream")` | Uploads `list[int]` buffer data and returns success as `bool`. |
| `tryPutBuffer(key, data, contentType="application/octet-stream")` | Result-returning buffer upload. |
| `putStream(key, stream, contentType="application/octet-stream")` | Uploads a Go `io.Reader` value and returns success as `bool`. |
| `tryPutStream(key, stream, contentType="application/octet-stream")` | Result-returning stream upload for Go interop. |
| `getText(key)` | Reads an object as text, returning `""` on failure. |
| `tryGetText(key)` | Reads an object as text and returns `Result.Ok(str)` or `S3GetError`. |
| `getBytes(key)` | Alias for `getText` for byte-oriented object names. |
| `tryGetBytes(key)` | Alias for `tryGetText`. |
| `getBuffer(key)` | Reads an object into `list[int]`, returning `[]` on failure. |
| `tryGetBuffer(key)` | Result-returning buffer download. |
| `uploadFile(localPath, key, contentType="application/octet-stream")` | Uploads a local file and returns success as `bool`. |
| `tryUploadFile(localPath, key, contentType="application/octet-stream")` | Result-returning file upload. |
| `downloadFile(key, localPath)` | Downloads an object to a local file and returns success as `bool`. |
| `tryDownloadFile(key, localPath)` | Result-returning file download. |
| `exists(key)` | Checks whether an object exists. |
| `tryExists(key)` | Returns `Result.Ok(bool)` for existence checks. |
| `list(prefix="")` | Lists objects and returns `list[S3Object]`. |
| `tryList(prefix="")` | Result-returning object listing. |
| `listKeys(prefix="")` | Lists object keys only. |
| `tryListKeys(prefix="")` | Result-returning key-only listing. |
| `stat(key)` | Reads object metadata with HEAD, returning an empty `S3Object` on failure. |
| `tryStat(key)` | Result-returning object metadata lookup. |
| `delete(key)` | Deletes an object and returns success as `bool`. |
| `tryDelete(key)` | Result-returning single-object delete. |
| `deleteMany(keys)` | Deletes many objects and returns success as `bool`. |
| `tryDeleteMany(keys)` | Deletes many objects and returns `Result.Ok(count)`. |
| `copy(sourceKey, destKey, contentType="")` | Copies an object within the configured bucket. |
| `tryCopy(sourceKey, destKey, contentType="")` | Result-returning copy. |
| `move(sourceKey, destKey, contentType="")` | Copies then deletes the original object. |
| `tryMove(sourceKey, destKey, contentType="")` | Result-returning move. |
| `presignGet(key, expiresSeconds=900)` | Returns `Result.Ok(url)` for a temporary GET URL. |
| `presignedGetUrl(key, expiresSeconds=900)` | String fallback wrapper around `presignGet`. |
| `presignPut(key, contentType="application/octet-stream", expiresSeconds=900)` | Returns `Result.Ok(url)` for a temporary PUT URL. |
| `presignedPutUrl(key, contentType="application/octet-stream", expiresSeconds=900)` | String fallback wrapper around `presignPut`. |
| `publicUrl(key)` | Builds a public URL with the configured `publicEndpoint`. |

### `S3Object`

Objects returned by `list` expose:

| Field | Description |
|-------|-------------|
| `key` | Object key. |
| `size` | Object size in bytes. |
| `etag` | Provider ETag value. |
| `lastModified` | ISO-like timestamp string. |
| `contentType` | Content type from `stat` / `tryStat`; empty for plain list results. |
| `metadata` | User metadata from `stat` / `tryStat`; empty for plain list results. |

### `S3`

| Method | Description |
|--------|-------------|
| `S3.publicUrl(publicEndpoint, key)` | Builds a public URL without constructing a client. |
| `S3.hasEnv()` | Returns whether required environment configuration is present. |

## Testing

Offline tests compile and run without credentials:

```bash
/usr/bin/python3 third_party/lams3/tests/run_lams3_tests.py --verbose
```

Live tests use the dedicated `lams3-tests/live-roundtrip/` prefix and perform
upload, read, copy, move, list, file upload/download, presigned URL generation,
and bulk delete operations against the configured bucket:

```bash
export LAMS3_LIVE_TESTS=1
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_ENDPOINT="..."
export S3_PUBLIC_ENDPOINT="..."
export S3_REGION="auto"
export S3_BUCKET="..."
/usr/bin/python3 third_party/lams3/tests/run_lams3_tests.py --live --verbose
```

## Notes

- `publicUrl` trims `http://` or `https://` from the configured public endpoint
  and always returns an `https://` URL.
- Object keys are path-escaped while `/` remains a path separator.
- The historical convenience methods return empty strings or `false` on failed
  network operations. Prefer `try*` methods when callers need the error.
- The package currently pins AWS SDK for Go v2 modules including
  `github.com/aws/aws-sdk-go-v2/service/s3 v1.105.0`.

## License and changelog

lams3 is Copyright 2026 Thallium Solutions di Busconi Alessandro and is
distributed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE`
in this directory. Package-specific release notes live in `CHANGELOG.md`.
