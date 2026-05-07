# lams3

S3-compatible object storage client for Lammergeier. It is designed as a standalone third-party package so it can be moved to its own repository and installed with `lamc install`.

## Layout

```text
lams3/
├── __init__.lam
├── lamlib.toml
├── README.md
├── .env.example
└── tests/
```

## Configuration

The library reads credentials from environment variables. Do not commit real secrets.

```bash
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_ENDPOINT="account-id.r2.cloudflarestorage.com"
export S3_PUBLIC_ENDPOINT="bucket.example.com"
export S3_REGION="auto"
export S3_PUBLIC_BUCKET="bucket-name"
export S3_USE_PATH_STYLE="true"
```

## Example

```lammergeier
from lams3 import S3Client

func main() {
    s3: S3Client = S3Client.fromEnv()
    s3.putText("hello.txt", "hello from Lam", "text/plain")
    print(s3.getText("hello.txt"))
    print(s3.publicUrl("hello.txt"))
    s3.delete("hello.txt")
}
```

## Local tests

Offline helper tests do not need credentials:

```bash
/usr/bin/python3 ../../tests/tests/run_tests.py --dir third_party/lams3/tests --filter offline --verbose
```

Live S3/R2 tests need credentials and perform create/read/list/delete operations:

```bash
export LAMS3_LIVE_TESTS=1
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_ENDPOINT="..."
export S3_PUBLIC_ENDPOINT="..."
export S3_REGION="auto"
export S3_PUBLIC_BUCKET="..."
/usr/bin/python3 ../../tests/tests/run_tests.py --dir third_party/lams3/tests --filter live --verbose
```
