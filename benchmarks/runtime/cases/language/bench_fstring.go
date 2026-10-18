// Hand-written Go baseline for bench_fstring.lam (Lam's int "/" yields a float).
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	last := ""
	for i := 0; i < 200000; i++ {
		last = fmt.Sprintf("iter=%d half=%v name=user_%d tag=bench", i, float64(i)/2, i)
	}
	if len(last) == 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH fstring 200000 %d\n", time.Since(start).Nanoseconds())
}
