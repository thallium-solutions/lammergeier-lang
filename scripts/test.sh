#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}

usage() {
    cat <<'EOF'
Usage:
  sh scripts/test.sh                 Run the full local quality gate
  sh scripts/test.sh core [filter]
  sh scripts/test.sh semantic [filter]
  sh scripts/test.sh syntax [filter]
  sh scripts/test.sh transpile [filter]
  sh scripts/test.sh lsp
  sh scripts/test.sh import
  sh scripts/test.sh unit
  sh scripts/test.sh rosetta [filter]
EOF
}

run_suite() {
    suite=$1
    filter=${2:-}

    case "$suite" in
        core)
            if [ -n "$filter" ]; then
                "$PYTHON" tests/tests/run_tests.py --filter "$filter"
            else
                "$PYTHON" tests/tests/run_tests.py
            fi
            ;;
        semantic)
            if [ -n "$filter" ]; then
                "$PYTHON" tests/semantic/run_semantic_tests.py --filter "$filter"
            else
                "$PYTHON" tests/semantic/run_semantic_tests.py
            fi
            ;;
        syntax)
            if [ -n "$filter" ]; then
                "$PYTHON" tests/syntax/run_syntax_tests.py --filter "$filter"
            else
                "$PYTHON" tests/syntax/run_syntax_tests.py
            fi
            ;;
        transpile)
            if [ -n "$filter" ]; then
                "$PYTHON" tests/transpilation/run_transpilation_tests.py --filter "$filter"
            else
                "$PYTHON" tests/transpilation/run_transpilation_tests.py
            fi
            ;;
        lsp)
            if [ -n "$filter" ]; then
                echo "error: lsp suite does not support filters" >&2
                usage >&2
                exit 2
            fi
            "$PYTHON" tests/lsp/test_lsp.py
            ;;
        import)
            if [ -n "$filter" ]; then
                echo "error: import suite does not support filters" >&2
                usage >&2
                exit 2
            fi
            "$PYTHON" tests/import_resolution/run_import_resolution_tests.py
            ;;
        unit)
            if [ -n "$filter" ]; then
                echo "error: unit suite does not support filters" >&2
                usage >&2
                exit 2
            fi
            "$PYTHON" tests/tests/test_diagnostics.py
            "$PYTHON" tests/tests/test_semantic_warnings.py
            "$PYTHON" tests/tests/test_source_map.py
            "$PYTHON" tests/tests/test_modules.py
            "$PYTHON" tests/tests/test_ast_builder.py
            "$PYTHON" tests/tests/test_typesys.py
            "$PYTHON" tests/tests/test_typed_ir.py
            "$PYTHON" tests/tests/test_lowering_contracts.py
            "$PYTHON" tests/tests/test_formatter.py
            "$PYTHON" tests/tests/test_vscode_grammar.py
            "$PYTHON" tests/tests/test_multiline_calls.py
            "$PYTHON" tests/tests/test_go_pins.py
            "$PYTHON" tests/tests/test_go_dependency_diagnostics.py
            "$PYTHON" tests/tests/test_go_error_mapping.py
            "$PYTHON" tests/tests/test_generated_go_contracts.py
            "$PYTHON" tests/tests/test_go_library_filenames.py
            "$PYTHON" tests/tests/test_doctor.py
            "$PYTHON" tests/tests/test_lib_run.py
            "$PYTHON" tests/tests/test_github_lams3_install.py
            "$PYTHON" -m pytest tests/tests/test_collection_context_fuzz.py tests/tests/test_random_program_fuzz.py -q
            ;;
        rosetta)
            if [ -n "$filter" ]; then
                "$PYTHON" tests/rosetta_tests/run_rosetta.py --filter "$filter"
            else
                "$PYTHON" tests/rosetta_tests/run_rosetta.py
            fi
            ;;
        *)
            echo "error: unknown test suite '$suite'" >&2
            usage >&2
            exit 2
            ;;
    esac
}

cd "$ROOT"

if [ "$#" -eq 0 ]; then
    run_suite semantic
    run_suite syntax
    run_suite transpile
    run_suite lsp
    run_suite import
    run_suite unit
    run_suite core
    run_suite rosetta
    exit 0
fi

if [ "$#" -gt 2 ]; then
    echo "error: too many arguments" >&2
    usage >&2
    exit 2
fi

run_suite "$1" "${2:-}"
