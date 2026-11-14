#!/usr/bin/env python3
"""The character helpers of ``str`` (``_STR_RUNTIME_GO`` in
compiler/transpiler.py) against a reference model, in Go.

The model is Go's own walk of a string (``for b := range s``: a character
per code point, an invalid byte being a character of its own), so the
helpers' fast paths (ASCII runs, the cached character index of long
strings, the loop-invariant views ``lamStrViewOf`` / ``lamStrAtV`` ...)
must agree with it on every string: ASCII, mixed, non-ASCII only, invalid
UTF-8, short and long (the index), at every index and slice bound
(negative, past either end). The checks run on several goroutines at
once (the index cache is shared) and twice per string (cache misses and
hits)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.transpiler import GoTranspiler  # noqa: E402

_TEST_GO = r'''
package strruntime

import (
	"fmt"
	"math/rand"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"
)

// The reference: the byte offset of every character, then len(s).
func refOffsets(s string) []int {
	var out []int
	for b := range s {
		out = append(out, b)
	}
	return append(out, len(s))
}

func refBound(n, i int) int {
	if i < 0 {
		i += n
		if i < 0 {
			return 0
		}
	}
	if i > n {
		return n
	}
	return i
}

func refSlice(s string, offs []int, a, b int) string {
	n := len(offs) - 1
	lo, hi := refBound(n, a), refBound(n, b)
	if hi <= lo {
		return ""
	}
	return s[offs[lo]:offs[hi]]
}

func refASCII(s string) int {
	i := 0
	for i < len(s) && s[i] < utf8.RuneSelf {
		i++
	}
	return i
}

func refStep(s string, start, end, step int, sp, ep bool) string {
	rs := []rune(s)
	n := len(rs)
	start, end = lamStrStepBounds(n, start, end, step, sp, ep)
	var out []rune
	if step > 0 {
		for i := start; i < end; i += step {
			out = append(out, rs[i])
		}
	} else {
		for i := start; i > end; i += step {
			out = append(out, rs[i])
		}
	}
	return string(out)
}

// at calls f, reporting an IndexError panic as ok == false.
func at(f func() string) (v string, ok bool) {
	defer func() {
		if r := recover(); r != nil {
			if _, isIndex := r.(lamStrIndexError); !isIndex {
				panic(r)
			}
			ok = false
		}
	}()
	return f(), true
}

var pieces = []string{
	"a", "b", " ", ",", "z", "0", "~", "\x00", "\x7f",
	"é", "ü", "€", "😀", "\u0301", "中",
	"\xff", "\x80", "\xc3", "\xe2\x82", "\xf0\x9f\x98",
}

func randomString(r *rand.Rand) string {
	var sb strings.Builder
	n := r.Intn(160)
	if r.Intn(4) == 0 {
		// A long ASCII run, then (maybe) anything.
		sb.WriteString(strings.Repeat("abcdefgh", 16)[:64+r.Intn(60)])
		n = r.Intn(20)
	}
	ascii := r.Intn(3) == 0
	for i := 0; i < n; i++ {
		if ascii {
			sb.WriteString(pieces[r.Intn(9)])
		} else {
			sb.WriteString(pieces[r.Intn(len(pieces))])
		}
	}
	return sb.String()
}

func corpus() []string {
	r := rand.New(rand.NewSource(7))
	out := []string{"", "a", "é", "\xff", strings.Repeat("x", 64), strings.Repeat("x", 65),
		strings.Repeat("x", 65) + "é", "é" + strings.Repeat("x", 80), strings.Repeat("€", 70),
		strings.Repeat("ab", 40) + "\xff" + strings.Repeat("cd", 10)}
	for i := 0; i < 400; i++ {
		out = append(out, randomString(r))
	}
	return out
}

func check(t *testing.T, s string) {
	where := fmt.Sprintf("%q", s)
	offs := refOffsets(s)
	n := len(offs) - 1
	if got := lamStrASCII(s); got != refASCII(s) {
		t.Errorf("lamStrASCII(%s) = %d, want %d", where, got, refASCII(s))
	}
	if got := lamStrLen(s); got != n {
		t.Errorf("lamStrLen(%s) = %d, want %d", where, got, n)
	}
	view := lamStrViewOf(s)
	if view.head != refASCII(s) || view.n != n || view.s != s {
		t.Errorf("lamStrViewOf(%s) = head %d, n %d; want %d, %d", where, view.head, view.n, refASCII(s), n)
	}
	// The view of a loop, and a bare one (no ASCII run known, no marks:
	// every read goes to the plain helpers), give the same results.
	views := []*lamStrIndex{&view, {s: s, n: n}}
	for k, v := range views {
		if got := lamStrLenV(v); got != n {
			t.Errorf("lamStrLenV(%s, view %d) = %d, want %d", where, k, got, n)
		}
	}
	for i := -n - 3; i <= n+3; i++ {
		want, wantOK := "", false
		if j := i; j >= -n && j < n {
			if j < 0 {
				j += n
			}
			want, wantOK = s[offs[j]:offs[j+1]], true
		}
		got, ok := at(func() string { return lamStrAt(s, i) })
		if ok != wantOK || got != want {
			t.Errorf("lamStrAt(%s, %d) = %q, %v; want %q, %v", where, i, got, ok, want, wantOK)
		}
		for k, v := range views {
			got, ok := at(func() string { return lamStrAtV(s, i, v) })
			if ok != wantOK || got != want {
				t.Errorf("lamStrAtV(%s, %d, view %d) = %q, %v; want %q, %v", where, i, k, got, ok, want, wantOK)
			}
		}
		if got, want := lamStrFrom(s, i), refSlice(s, offs, i, n); got != want {
			t.Errorf("lamStrFrom(%s, %d) = %q, want %q", where, i, got, want)
		}
		for k, v := range views {
			if got, want := lamStrFromV(s, i, v), refSlice(s, offs, i, n); got != want {
				t.Errorf("lamStrFromV(%s, %d, view %d) = %q, want %q", where, i, k, got, want)
			}
		}
	}
	bounds := []int{-n - 2, -n, -n + 1, -3, -1, 0, 1, 2, 3, 4, 5, n / 2, n - 4, n - 1, n, n + 1, n + 5}
	for _, a := range bounds {
		for _, b := range bounds {
			want := refSlice(s, offs, a, b)
			if got := lamStrSlice(s, a, b); got != want {
				t.Errorf("lamStrSlice(%s, %d, %d) = %q, want %q", where, a, b, got, want)
			}
			for k, v := range views {
				if got := lamStrSliceV(s, a, b, v.head, v); got != want {
					t.Errorf("lamStrSliceV(%s, %d, %d, view %d) = %q, want %q", where, a, b, k, got, want)
				}
			}
		}
	}
	for _, step := range []int{-3, -1, 1, 2} {
		for _, a := range []int{-2, 0, 3} {
			for _, b := range []int{-1, 0, 4} {
				for _, sp := range []bool{false, true} {
					for _, ep := range []bool{false, true} {
						want := refStep(s, a, b, step, sp, ep)
						if got := lamStrStep(s, a, b, step, sp, ep); got != want {
							t.Errorf("lamStrStep(%s, %d, %d, %d, %v, %v) = %q, want %q", where, a, b, step, sp, ep, got, want)
						}
					}
				}
			}
		}
	}
}

func TestStrHelpersMatchTheModel(t *testing.T) {
	strs := corpus()
	var wg sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for k := range strs {
				s := strs[(k+g*97)%len(strs)]
				check(t, s)
				check(t, s) // the cached index this time
			}
		}(g)
	}
	wg.Wait()
}

func TestASCIIWindows(t *testing.T) {
	// A non-ASCII byte at every position of a run longer than the windows.
	for n := 0; n < 100; n++ {
		for k := 0; k <= n; k++ {
			b := []byte(strings.Repeat("q", n))
			if k < n {
				b[k] = 0x80 | byte(k)
			}
			s := string(b)
			if got, want := lamStrASCII(s), refASCII(s); got != want {
				t.Fatalf("lamStrASCII(%q) = %d, want %d", s, got, want)
			}
			if got, want := lamStrLen(s), utf8.RuneCountInString(s); got != want {
				t.Fatalf("lamStrLen(%q) = %d, want %d", s, got, want)
			}
		}
	}
}
'''


def _package_source() -> str:
    return (
        "package strruntime\n\nimport (\n\t\"strconv\"\n\t\"sync/atomic\"\n\t\"unicode/utf8\"\n\t\"unsafe\"\n)\n"
        + GoTranspiler._STR_RUNTIME_GO
    )


def test_str_helpers_match_the_model() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_str_runtime_") as tmp:
        root = Path(tmp)
        (root / "go.mod").write_text("module strruntime\n\ngo 1.22\n", encoding="utf-8")
        (root / "runtime.go").write_text(_package_source(), encoding="utf-8")
        (root / "runtime_test.go").write_text(_TEST_GO, encoding="utf-8")
        run = subprocess.run(["go", "test", "-count=1", "./..."], cwd=root,
                             capture_output=True, text=True, timeout=600)
        assert run.returncode == 0, (run.stdout + run.stderr)[-6000:]
        print("PASS: the str helpers match the character model")


if __name__ == "__main__":
    if shutil.which("go") is None:
        print("SKIP: go is not installed")
        sys.exit(0)
    test_str_helpers_match_the_model()
