// Hand-written Go baseline for bench_none_checks.lam (no None checks).
package main

import (
	"fmt"
	"time"
)

type Node struct {
	value int
	next  *Node
}

func (n *Node) Val() int { return n.value }

func build(count int) *Node {
	var head *Node
	for i := 0; i < count; i++ {
		head = &Node{value: i, next: head}
	}
	return head
}

func total(head *Node) int {
	sum := 0
	for n := head; n != nil; n = n.next {
		sum += n.Val() + n.value
	}
	return sum
}

func main() {
	start := time.Now()
	head := build(1000)
	acc := 0
	for r := 0; r < 2000; r++ {
		acc += total(head)
	}
	if acc != 2000*999000 {
		fmt.Println("unreachable")
	}
	fmt.Printf("BENCH none_checks 2000000 %d\n", time.Since(start).Nanoseconds())
}
