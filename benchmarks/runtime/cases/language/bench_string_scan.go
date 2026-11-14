// Hand-written Go baseline for bench_string_scan.lam (ASCII: bytes are
// characters).
package main

import (
	"fmt"
	"strings"
	"time"
)

func main() {
	start := time.Now()
	src := strings.Repeat("alpha beta, gamma delta; epsilon zeta eta theta iota kappa lambda mu.\n", 310)
	words := 0
	for r := 0; r < 100; r++ {
		inWord := false
		for pos := 0; pos < len(src); pos++ {
			c := src[pos]
			if c == ' ' || c == ',' || c == ';' || c == '.' || c == '\n' {
				inWord = false
			} else if !inWord {
				inWord = true
				words++
			}
		}
	}
	if words == 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH string_scan 2170000 %d\n", time.Since(start).Nanoseconds())
}
