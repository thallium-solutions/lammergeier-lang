// Hand-written Go baseline for bench_dict_access.lam.
package main

import (
	"fmt"
	"time"
)

func main() {
	start := time.Now()
	d := map[string]int{}
	keys := []string{}
	for i := 0; i < 100000; i++ {
		k := fmt.Sprintf("k%d", i)
		keys = append(keys, k)
		d[k] = i
	}
	total := 0
	for _, k := range keys {
		total += d[k]
	}
	if total < 0 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH dict_access 100000 %d\n", time.Since(start).Nanoseconds())
}
