// Hand-written Go baseline for bench_list_append.lam.
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	items := []int{}
	for i := 0; i < 1000000; i++ {
		items = append(items, i)
	}
	total := 0
	for _, v := range items {
		total += v
	}
	if total < 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH list_append 1000000 %d\n", time.Since(start).Nanoseconds())
}
