// Hand-written Go baseline for bench_list_index.lam.
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	n := 1000
	xs := []int{}
	idx := []int{}
	for i := 0; i < n; i++ {
		xs = append(xs, i)
		idx = append(idx, (i*7919)%n)
	}
	total := 0
	for r := 0; r < 50000; r++ {
		for k := 0; k < n; k++ {
			j := idx[k]
			total += xs[j]
			xs[j] = total % 1024
		}
	}
	if total < 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH list_index 50000000 %d\n", time.Since(start).Nanoseconds())
}
