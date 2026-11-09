// Hand-written Go baseline for bench_fstring_str.lam: string concatenation.
package main

import (
	"fmt"
	"runtime"
	"strings"
	"time"
)

func key(entry, size string) string { return entry + "_" + size + ".avif" }

func url(base, entry, size string) string { return base + "/" + key(entry, size) }

func variant(entry, size string) string {
	dot := strings.LastIndex(entry, ".")
	return entry[:dot] + "_" + size + entry[dot:]
}

func allocated() int {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	return int(ms.TotalAlloc)
}

func work(entries, sizes []string, base string) int {
	total := 0
	for i := 0; i < 200000; i++ {
		entry := entries[i%len(entries)]
		size := sizes[i%len(sizes)]
		total += len(url(base, entry, size))
		total += len(variant(entry, size))
		total += len(size + ":" + entry)
	}
	return total
}

func main() {
	entries := []string{"0192f1a2-7c3b-7d4e-9f00-1a2b3c4d5e6f.jpg", "0192f1a2-7c3b-7d4e-9f00-6f5e4d3c2b1a.png"}
	sizes := []string{"thumb", "card", "full"}
	base := "https://media.example.com/public"
	expected := work(entries, sizes, base)
	before := allocated()
	start := time.Now()
	if work(entries, sizes, base) != expected {
		fmt.Println("unreachable")
	}
	elapsed := time.Since(start).Nanoseconds()
	perOp := (allocated() - before) / 200000
	fmt.Printf("BENCH fstring_str 200000 %d %d\n", elapsed, perOp)
}
