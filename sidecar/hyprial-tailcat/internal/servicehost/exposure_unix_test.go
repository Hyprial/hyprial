//go:build !windows

package servicehost

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

type exposureInfo struct {
	os.FileInfo
	owner syscall.Stat_t
}

func (i exposureInfo) Sys() any { return &i.owner }

func TestExposurePrivateV2(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	target := "unix:" + filepath.Join(dir, "backend.sock")
	check := func(target, proxy string) error {
		return validateExposure(Exposure{Port: 8447, Target: target, ProxyProtocol: proxy})
	}
	t.Run("private-current-user", func(t *testing.T) {
		if err := check(target, "v2"); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("tcp", func(t *testing.T) {
		if err := check("tcp:127.0.0.1:8080", "v2"); err == nil {
			t.Fatal("accepted v2 over TCP")
		}
	})
	t.Run("public-directory", func(t *testing.T) {
		if err := os.Chmod(dir, 0755); err != nil {
			t.Fatal(err)
		}
		defer os.Chmod(dir, 0700)
		if err := check(target, "v2"); err == nil {
			t.Fatal("accepted v2 in 0755 directory")
		}
		if err := check(target, ""); err != nil {
			t.Fatalf("non-v2 changed: %v", err)
		}
	})
	t.Run("foreign-owner", func(t *testing.T) {
		original := exposureLstat
		defer func() { exposureLstat = original }()
		exposureLstat = func(path string) (os.FileInfo, error) {
			info, err := original(path)
			if err != nil {
				return nil, err
			}
			owner := *info.Sys().(*syscall.Stat_t)
			owner.Uid = uint32(os.Getuid()) + 1
			return exposureInfo{info, owner}, nil
		}
		if err := check(target, "v2"); err == nil {
			t.Fatal("accepted v2 in foreign-owned directory")
		}
	})
	t.Run("symlink-directory", func(t *testing.T) {
		link := filepath.Join(t.TempDir(), "link")
		if err := os.Symlink(dir, link); err != nil {
			t.Fatal(err)
		}
		linked := "unix:" + filepath.Join(link, "backend.sock")
		if err := check(linked, "v2"); err == nil {
			t.Fatal("accepted v2 through symlink directory")
		}
		if err := check(linked, ""); err != nil {
			t.Fatalf("non-v2 changed: %v", err)
		}
	})
	t.Run("unavailable-directory", func(t *testing.T) {
		if err := check("unix:"+filepath.Join(dir, "absent", "backend.sock"), "v2"); err == nil {
			t.Fatal("accepted v2 in missing directory")
		}
	})
	t.Run("non-v2-tcp", func(t *testing.T) {
		if err := check("tcp:127.0.0.1:8080", ""); err != nil {
			t.Fatal(err)
		}
	})
}
