// OTT v1 Go bot. Read one observation line and return one legal action line.
// Keep stdout reserved for actions. Write diagnostics to stderr.
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"
)

const maxLineBytes = 1 << 20

func chooseAction(observation string) string {
	key := `"legal_actions"`
	keyAt := strings.Index(observation, key)
	if keyAt < 0 {
		return ""
	}
	arrayAt := strings.IndexByte(observation[keyAt+len(key):], '[')
	if arrayAt < 0 {
		return ""
	}
	start := keyAt + len(key) + arrayAt + 1
	for start < len(observation) && (observation[start] == ' ' || observation[start] == '\t' || observation[start] == '\r' || observation[start] == '\n') {
		start++
	}
	if start == len(observation) || observation[start] != '{' {
		return ""
	}
	depth, quoted, escaped := 0, false, false
	for i := start; i < len(observation); i++ {
		c := observation[i]
		if quoted {
			if escaped {
				escaped = false
			} else if c == '\\' {
				escaped = true
			} else if c == '"' {
				quoted = false
			}
			continue
		}
		switch c {
		case '"':
			quoted = true
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				return observation[start : i+1]
			}
		}
	}
	return ""
}

func main() {
	reader := bufio.NewReaderSize(os.Stdin, 64*1024)
	writer := bufio.NewWriter(os.Stdout)
	for {
		line, err := reader.ReadString('\n')
		if len(line) > maxLineBytes || (err == nil && len(line) == 0) {
			fmt.Fprintln(os.Stderr, "observation exceeds line limit")
			os.Exit(1)
		}
		if err != nil && err != io.EOF {
			fmt.Fprintln(os.Stderr, "input read failed")
			os.Exit(1)
		}
		if len(line) > 0 {
			if err == io.EOF || line[len(line)-1] != '\n' {
				fmt.Fprintln(os.Stderr, "observation must end with LF")
				os.Exit(1)
			}
			action := chooseAction(strings.TrimSuffix(line, "\n"))
			if action == "" {
				fmt.Fprintln(os.Stderr, "no legal action")
				os.Exit(1)
			}
			if _, writeErr := writer.WriteString(action + "\n"); writeErr != nil || writer.Flush() != nil {
				os.Exit(1)
			}
		}
		if err == io.EOF {
			break
		}
	}
}
