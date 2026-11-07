// Synthetic Unix IPC client for AT08.2a's real VM probe, not a harness.
// Build: CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o client this-file.go
package main

import (
	"bufio"
	"fmt"
	"net"
	"os"
	"time"
)

func main() {
	if len(os.Args) != 3 {
		panic("usage: client socket requests.jsonl")
	}
	f, err := os.Open(os.Args[2])
	if err != nil {
		panic(err)
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		conn, err := net.DialTimeout("unix", os.Args[1], 5*time.Second)
		if err != nil {
			panic(err)
		}
		conn.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := fmt.Fprintln(conn, scanner.Text()); err != nil {
			panic(err)
		}
		response, err := bufio.NewReader(conn).ReadString('\n')
		conn.Close()
		if err != nil {
			panic(err)
		}
		fmt.Print(response)
	}
	if err := scanner.Err(); err != nil {
		panic(err)
	}
}
