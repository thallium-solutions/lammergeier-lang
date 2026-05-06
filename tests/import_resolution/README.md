# Import resolution diagnostics

These tests exercise compiler diagnostics that require the Lammergeier module search path, such as missing module errors. They intentionally live outside `tests/semantic/` because module resolution happens after parsing and semantic checking.
