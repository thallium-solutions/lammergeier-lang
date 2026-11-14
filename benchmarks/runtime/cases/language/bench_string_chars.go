// Hand-written Go baseline for bench_string_chars.lam (ASCII: byte
// indexing gives the same characters).
package main

import (
	"fmt"
	"time"
)

func reverse(s string) string {
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		b[len(s)-1-i] = s[i]
	}
	return string(b)
}

func main() {
	start := time.Now()
	text := "The quick brown fox jumps over the lazy dog; pack my box with five dozen liquor jugs!!!!"
	total := 0
	for r := 0; r < 25000; r++ {
		n := len(text)
		for i := 0; i < 80; i++ {
			if text[i] == text[n-1-i] {
				total++
			}
			if text[i:i+2] == "o " {
				total += 2
			}
		}
		total += len(text[3:20]) + len(text[len(text)-5:]) + len(reverse(text))
	}
	if total == 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH string_chars 2000000 %d\n", time.Since(start).Nanoseconds())
}
