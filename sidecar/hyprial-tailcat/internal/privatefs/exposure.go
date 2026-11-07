package privatefs

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ProxyTarget requires an identity-carrying exposure to use a Unix socket
// inside an existing, non-symlink, current-user 0700 directory. lstat is
// supplied by each validator so ownership failures can be tested without root.
// This is an admission-time check, not a filesystem race protection mechanism.
func ProxyTarget(target string, lstat func(string) (os.FileInfo, error)) error {
	if !strings.HasPrefix(target, "unix:") {
		return errors.New("proxyProtocol v2 requires a private unix target")
	}
	path := strings.TrimPrefix(target, "unix:")
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return errors.New("proxyProtocol v2 requires a clean absolute unix target")
	}
	info, err := lstat(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("proxyProtocol v2 target directory unavailable: %w", err)
	}
	if info.Mode()&(os.ModeSymlink|os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return errors.New("proxyProtocol v2 requires a non-symlink 0700 directory")
	}
	if err := directoryInfo(info); err != nil {
		return fmt.Errorf("proxyProtocol v2 target directory: %w", err)
	}
	return nil
}
