# lams3

`lams3` is a small Lammergeier package for S3-compatible object storage.
It targets AWS S3-style APIs and S3-compatible providers such as Cloudflare R2,
MinIO, Wasabi, Backblaze B2, and custom gateways.

It is intentionally packaged under `third_party/` with its own `lamlib.toml`,
tests, and README so it can be used as a normal external Lam library.

## What you get

- **Simple text object I/O** with `putText`, `getText`, `exists`, `list`, and
  `delete`.
- **Environment-based setup** through `S3Config.fromEnv()` and
  `S3Client.fromEnv()`.
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
├── .env.example              # Safe environment template
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
export S3_PUBLIC_BUCKET="bucket-name"
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
| `S3_PUBLIC_BUCKET` | Yes | Bucket used by client operations. |
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
| `S3Config.fromEnv()` | Builds config from `S3_*` environment variables. |
| `cfg.isReady()` | Returns `true` when required credentials, endpoint, region, and bucket are present. |

### `S3Client`

| Method | Description |
|--------|-------------|
| `S3Client.fromEnv()` | Creates a client from environment configuration. |
| `putText(key, text, contentType="text/plain")` | Uploads text and returns success as `bool`. |
| `getText(key)` | Reads an object as text, returning `""` on failure. |
| `exists(key)` | Checks whether an object exists. |
| `list(prefix="")` | Lists objects and returns `list[S3Object]`. |
| `delete(key)` | Deletes an object and returns success as `bool`. |
| `publicUrl(key)` | Builds a public URL with the configured `publicEndpoint`. |

### `S3Object`

Objects returned by `list` expose:

| Field | Description |
|-------|-------------|
| `key` | Object key. |
| `size` | Object size in bytes. |
| `etag` | Provider ETag value. |
| `lastModified` | ISO-like timestamp string. |

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

Live tests perform upload, read, list, URL generation, and delete operations
against the configured bucket:

```bash
export LAMS3_LIVE_TESTS=1
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_ENDPOINT="..."
export S3_PUBLIC_ENDPOINT="..."
export S3_REGION="auto"
export S3_PUBLIC_BUCKET="..."
/usr/bin/python3 third_party/lams3/tests/run_lams3_tests.py --live --verbose
```

## Notes

- `publicUrl` trims `http://` or `https://` from the configured public endpoint
  and always returns an `https://` URL.
- Object keys are path-escaped while `/` remains a path separator.
- Failed network operations return empty strings or `false` instead of raising
  Lam exceptions.
