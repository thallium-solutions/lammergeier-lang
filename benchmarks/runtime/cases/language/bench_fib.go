// Hand-written Go baseline for bench_fib.lam (same workload and output).
package main

import (
	"fmt"
	"time"
)

func fib(n int) int {
	if n < 2 {
		return n
	}
	return fib(n-1) + fib(n-2)
}

func main() {
	start := time.Now()
	total := 0
	for i := 0; i < 5; i++ {
		total += fib(28)
	}
	if total < 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH fib 5 %d\n", time.Since(start).Nanoseconds())
}
