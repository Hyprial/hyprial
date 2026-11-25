//go:build windows

package privatefs

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"

	"golang.org/x/sys/windows"
)

// v2 exposure remains Unix-only for this channel. A Windows directory-type
// check must not be mistaken for the Unix identity-carrying exposure
// validation, so report the exposure path unavailable rather than silently
// succeeding. This preserves the existing v2 exposure gate on Windows.
func directoryInfo(os.FileInfo) error {
	return errors.New("privatefs: v2 exposure is unix-only on Windows")
}

// Directory checks a normal user directory without imposing a DACL or 0700
// identity gate. Missing paths, non-directories and reparse-point paths are
// rejected.
func Directory(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	return requireDirectory(info)
}

func requireDirectory(info os.FileInfo) error {
	if info == nil || !info.IsDir() || isReparsePoint(info) {
		return errors.New("privatefs: directory required")
	}
	return nil
}

// File checks a normal user file without imposing a DACL identity gate.
// Missing paths, non-regular files and reparse-point paths are rejected.
func File(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	return requireRegularFile(info)
}

func requireRegularFile(info os.FileInfo) error {
	if info == nil || !info.Mode().IsRegular() || isReparsePoint(info) {
		return errors.New("privatefs: regular file required")
	}
	return nil
}

func isReparsePoint(info os.FileInfo) bool {
	if info == nil {
		return false
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return true
	}
	if data, ok := info.Sys().(*syscall.Win32FileAttributeData); ok {
		return data.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0
	}
	return false
}

// Lock takes a real exclusive, non-blocking byte-range lock on the lock file.
// The file is opened without following a reparse point, and the opened handle
// is checked again so a path swap cannot turn the lock into a symlink target.
func Lock(path string) (func() error, error) {
	if err := Directory(filepath.Dir(path)); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(path); err == nil {
		if err := requireRegularFile(info); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}

	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	handle, err := windows.CreateFile(
		name,
		windows.GENERIC_READ|windows.GENERIC_WRITE,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE,
		nil,
		windows.OPEN_ALWAYS,
		windows.FILE_ATTRIBUTE_NORMAL|windows.FILE_FLAG_OPEN_REPARSE_POINT,
		0,
	)
	if err != nil {
		return nil, fmt.Errorf("privatefs: open lock file: %w", err)
	}
	var opened windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(handle, &opened); err != nil {
		_ = windows.CloseHandle(handle)
		return nil, fmt.Errorf("privatefs: inspect lock file: %w", err)
	}
	if opened.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		_ = windows.CloseHandle(handle)
		return nil, errors.New("privatefs: reparse-point lock file rejected")
	}

	var overlapped windows.Overlapped
	if err := windows.LockFileEx(
		handle,
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, &overlapped,
	); err != nil {
		_ = windows.CloseHandle(handle)
		return nil, fmt.Errorf("privatefs: lock file: %w", err)
	}
	return func() error {
		unlockErr := windows.UnlockFileEx(handle, 0, 1, 0, &overlapped)
		closeErr := windows.CloseHandle(handle)
		if unlockErr != nil {
			return fmt.Errorf("privatefs: unlock file: %w", unlockErr)
		}
		if closeErr != nil {
			return fmt.Errorf("privatefs: close lock file: %w", closeErr)
		}
		return nil
	}, nil
}

// Write performs a same-directory atomic durable replace. The previous file is
// untouched until MoveFileEx succeeds; every failure propagates and the
// temporary file is removed.
func Write(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := Directory(dir); err != nil {
		return fmt.Errorf("privatefs: write directory: %w", err)
	}
	if info, err := os.Lstat(path); err == nil {
		if err := requireRegularFile(info); err != nil {
			return err
		}
	} else if !os.IsNotExist(err) {
		return err
	}

	f, err := os.CreateTemp(dir, ".membership-*")
	if err != nil {
		return fmt.Errorf("privatefs: create temporary file: %w", err)
	}
	name := f.Name()
	defer os.Remove(name)

	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("privatefs: write temporary file: %w", err)
	}
	if err := windows.FlushFileBuffers(windows.Handle(f.Fd())); err != nil {
		_ = f.Close()
		return fmt.Errorf("privatefs: flush temporary file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("privatefs: close temporary file: %w", err)
	}

	from, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	to, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	if err := windows.MoveFileEx(from, to, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH); err != nil {
		return fmt.Errorf("privatefs: replace file: %w", err)
	}
	return nil
}
