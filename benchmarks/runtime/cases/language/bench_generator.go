// Hand-written Go baseline for bench_generator.lam: the generator is a
// range-over-func iterator (pulled with iter.Pull where the Lam program
// keeps the generator in a variable).
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

func main() {
	start := time.Now()
	total := 0
	for x := range numbers(400000) {
		total += x
	}
	evens := []int{}
	for x := range numbers(300000) {
		if x%2 == 0 {
			evens = append(evens, x)
		}
	}
	total += len(evens)
	next, stop := iter.Pull(numbers(300000))
	for {
		x, ok := next()
		if !ok {
			break
		}
		total += x
	}
	stop()
	if total != 124999800000 {
		fmt.Println("unexpected", total)
	}
	fmt.Printf("BENCH generator 1000000 %d\n", time.Since(start).Nanoseconds())
}
