// Hand-written Go baseline for bench_method_dispatch.lam.
package main

import (
	"fmt"
	"time"
)

type Counter struct{ value int }

func (c *Counter) Add(n int) { c.value += n }
func (c *Counter) Get() int  { return c.value }

func main() {
	start := time.Now()
	c := &Counter{}
	for i := 0; i < 2000000; i++ {
		c.Add(1)
	}
	if c.Get() != 2000000 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH method_dispatch 2000000 %d\n", time.Since(start).Nanoseconds())
}
