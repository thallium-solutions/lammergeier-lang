// Hand-written Go baseline for bench_generator_resume.lam: range-over-func
// iterators pulled one value at a time with iter.Pull, resumed by
// successive loops of 100 values.
package main

import (
	"fmt"
	"iter"
	"time"
)

func numbers(n int) iter.Seq[int] {
	return func(yield func(int) bool) {
		for i := 0; i < n; i++ {
			if !yield(i) {
				return
			}
		}
	}
}

func drain(next func() (int, bool), total int) int {
	for chunk := 0; chunk < 5000; chunk++ {
		for {
			x, ok := next()
			if !ok {
				break
			}
			total += x
			if x%100 == 99 {
				break
			}
		}
	}
	return total
}

func main() {
	start := time.Now()
	total := 0
	next, stop := iter.Pull(numbers(500000))
	total = drain(next, total)
	stop()
	next2, stop2 := iter.Pull(numbers(500000))
	total = drain(next2, total)
	stop2()
	if total != 249999500000 {
		fmt.Println("unexpected", total)
	}
	fmt.Printf("BENCH generator_resume 1000000 %d\n", time.Since(start).Nanoseconds())
}
