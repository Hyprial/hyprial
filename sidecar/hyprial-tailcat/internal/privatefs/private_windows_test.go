//go:build windows

package privatefs

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

func privateTestDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	state := filepath.Join(dir, "state")
	if err := os.Mkdir(state, 0700); err != nil {
		t.Fatalf("mkdir state: %v", err)
	}
	return state
}

func writeTestFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func assertNoTemporaryFiles(t *testing.T, dir string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read dir %s: %v", dir, err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".membership-") {
			t.Fatalf("temporary file left behind: %s", entry.Name())
		}
	}
}

func TestDirectoryAndFileChecks(t *testing.T) {
	dir := privateTestDir(t)
	if err := Directory(dir); err != nil {
		t.Fatalf("Directory(%q) = %v", dir, err)
	}
	missing := filepath.Join(dir, "missing")
	if err := Directory(missing); err == nil {
		t.Fatal("Directory accepted missing path")
	}
	if err := File(missing); err == nil {
		t.Fatal("File accepted missing path")
	}
	file := filepath.Join(dir, "state.json")
	writeTestFile(t, file, []byte("{}"))
	if err := File(file); err != nil {
		t.Fatalf("File(%q) = %v", file, err)
	}
	if err := Directory(file); err == nil {
		t.Fatal("Directory accepted regular file")
	}
	if err := File(dir); err == nil {
		t.Fatal("File accepted directory")
	}
	info, err := os.Lstat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := directoryInfo(info); err == nil {
		t.Fatal("directoryInfo silently accepted a Windows v2 exposure directory")
	}
}

func TestDirectoryRejectsReparsePoint(t *testing.T) {
	dir := privateTestDir(t)
	target := filepath.Join(dir, "target-dir")
	if err := os.Mkdir(target, 0700); err != nil {
		t.Fatalf("mkdir target: %v", err)
	}
	link := filepath.Join(dir, "link-dir")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("directory symlink unavailable on this host: %v", err)
	}
	if err := Directory(link); err == nil {
		t.Fatal("Directory accepted a directory reparse point")
	}
}

func TestFileRejectsReparsePoint(t *testing.T) {
	dir := privateTestDir(t)
	target := filepath.Join(dir, "target-file")
	writeTestFile(t, target, []byte("content"))
	link := filepath.Join(dir, "link-file")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("file symlink unavailable on this host: %v", err)
	}
	if err := File(link); err == nil {
		t.Fatal("File accepted a file reparse point")
	}
}

func TestLockExclusionAndReopen(t *testing.T) {
	dir := privateTestDir(t)
	lockPath := filepath.Join(dir, "state.lock")
	unlock, err := Lock(lockPath)
	if err != nil {
		t.Fatalf("first Lock = %v", err)
	}
	if _, err := Lock(lockPath); err == nil {
		t.Fatal("second exclusive lock succeeded")
	}
	if err := unlock(); err != nil {
		t.Fatalf("first unlock = %v", err)
	}
	unlock, err = Lock(lockPath)
	if err != nil {
		t.Fatalf("Lock after unlock = %v", err)
	}
	if err := unlock(); err != nil {
		t.Fatalf("second unlock = %v", err)
	}
}

func TestLockRejectsReparsePoint(t *testing.T) {
	dir := privateTestDir(t)
	target := filepath.Join(dir, "target.lock")
	writeTestFile(t, target, nil)
	link := filepath.Join(dir, "link.lock")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("file symlink unavailable on this host: %v", err)
	}
	if _, err := Lock(link); err == nil {
		t.Fatal("Lock accepted a reparse-point lock path")
	}
}

func TestWriteAtomicReplace(t *testing.T) {
	dir := privateTestDir(t)
	path := filepath.Join(dir, "state.json")
	if err := Write(path, []byte("first")); err != nil {
		t.Fatalf("first Write = %v", err)
	}
	if err := Write(path, []byte("second")); err != nil {
		t.Fatalf("second Write = %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read replaced file: %v", err)
	}
	if string(got) != "second" {
		t.Fatalf("replaced content = %q, want %q", got, "second")
	}
	assertNoTemporaryFiles(t, dir)
}

func TestWritePreservesPreviousFileOnReplaceFailure(t *testing.T) {
	dir := privateTestDir(t)
	path := filepath.Join(dir, "state.json")
	writeTestFile(t, path, []byte("good"))

	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		t.Fatal(err)
	}
	handle, err := windows.CreateFile(
		name,
		windows.GENERIC_READ,
		0,
		nil,
		windows.OPEN_EXISTING,
		windows.FILE_ATTRIBUTE_NORMAL,
		0,
	)
	if err != nil {
		t.Fatalf("open holding handle: %v", err)
	}
	if err := Write(path, []byte("replacement")); err == nil {
		_ = windows.CloseHandle(handle)
		t.Fatal("Write succeeded while the previous file was open without delete sharing")
	}
	if err := windows.CloseHandle(handle); err != nil {
		t.Fatalf("close holding handle: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read preserved file: %v", err)
	}
	if string(got) != "good" {
		t.Fatalf("preserved content = %q, want %q", got, "good")
	}
	assertNoTemporaryFiles(t, dir)

	if err := Write(path, []byte("replacement")); err != nil {
		t.Fatalf("Write after releasing handle = %v", err)
	}
	got, err = os.ReadFile(path)
	if err != nil {
		t.Fatalf("read replacement file: %v", err)
	}
	if string(got) != "replacement" {
		t.Fatalf("replacement content = %q, want %q", got, "replacement")
	}
}

func TestWriteRejectsNonRegularTarget(t *testing.T) {
	dir := privateTestDir(t)
	target := filepath.Join(dir, "target-dir")
	if err := os.Mkdir(target, 0700); err != nil {
		t.Fatalf("mkdir target: %v", err)
	}
	if err := Write(target, []byte("content")); err == nil {
		t.Fatal("Write accepted a directory target")
	}
	assertNoTemporaryFiles(t, dir)
}

func TestWriteRejectsMissingDirectory(t *testing.T) {
	dir := privateTestDir(t)
	path := filepath.Join(dir, "missing", "state.json")
	if err := Write(path, []byte("content")); err == nil {
		t.Fatal("Write accepted a missing directory")
	}
}
