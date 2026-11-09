// Hand-written Go baseline for bench_generator_break.lam: an endless
// range-over-func iterator left with break.
package main

import (
	"fmt"
	"iter"
	"time"
)

func naturals() iter.Seq[int] {
	return func(yield func(int) bool) {
		for i := 0; ; i++ {
			if !yield(i) {
				return
			}
		}
	}
}

func main() {
	start := time.Now()
	total := 0
	for k := 0; k < 10000; k++ {
		for x := range naturals() {
			if x == 10 {
				break
			}
			total += x
		}
	}
	if total != 450000 {
		fmt.Println("unexpected", total)
	}
	fmt.Printf("BENCH generator_break 10000 %d\n", time.Since(start).Nanoseconds())
}
