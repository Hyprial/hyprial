//go:build windows

package privatefs

import "errors"

// Managed membership needs a private Unix backend in v6. Refuse rather than
// silently substituting a loopback TCP path with different trust semantics.
func Directory(string) error { return errors.New("private Unix transport is unavailable") }
func File(string) error      { return errors.New("private Unix transport is unavailable") }
func Lock(string) (func() error, error) {
	return nil, errors.New("private Unix transport is unavailable")
}
func Write(string, []byte) error { return errors.New("private Unix transport is unavailable") }
