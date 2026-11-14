// Hand-written Go baseline for bench_string_scan_unicode.lam: ranges over
// the runes.
package main

import (
	"fmt"
	"strings"
	"time"
)

func main() {
	start := time.Now()
	src := strings.Repeat("alpha bêta, gamma délta; epsilon zêta eta thêta iota kappa lambda mü.\n", 310)
	words := 0
	for r := 0; r < 100; r++ {
		inWord := false
		for _, c := range src {
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
	fmt.Printf("BENCH string_scan_unicode 2201000 %d\n", time.Since(start).Nanoseconds())
}
