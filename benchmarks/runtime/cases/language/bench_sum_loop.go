// Hand-written Go baseline for bench_sum_loop.lam.
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	total := 0
	for i := 0; i < 10000000; i++ {
		total += i
	}
	if total == 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH sum_loop 10000000 %d\n", time.Since(start).Nanoseconds())
}
