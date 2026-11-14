// Hand-written Go baseline for bench_string_chars_unicode.lam: the string
// converted to []rune once per round, then indexed by character.
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	text := "Le cœur déçu mais l'âme plutôt naïve, Louÿs rêva de crapaüter en canoë au delà des îles, près du mälström où brûlent les novæ."
	total := 0
	for r := 0; r < 20000; r++ {
		rs := []rune(text)
		n := len(rs)
		for i := 0; i < 118; i++ {
			if rs[i] == rs[n-1-i] {
				total++
			}
			if string(rs[i:i+2]) == "o " {
				total += 2
			}
		}
		rev := make([]rune, n)
		for i, c := range rs {
			rev[n-1-i] = c
		}
		total += len(string(rs[3:20])) + len(string(rs[n-5:])) + len(string(rev))
	}
	if total == 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH string_chars_unicode 2360000 %d\n", time.Since(start).Nanoseconds())
}
