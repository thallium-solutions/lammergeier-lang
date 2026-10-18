// Hand-written Go baseline for bench_string_concat.lam (naive += like the Lam code).
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	s := ""
	for i := 0; i < 10000; i++ {
		s += "x"
	}
	if len(s) != 10000 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH string_concat 10000 %d\n", time.Since(start).Nanoseconds())
}
