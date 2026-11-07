// App-local canonical Tailcat node key generation.
//
// This file is the N1 additive ABI: one stateless export that creates an
// app-owned outbound node key and returns its canonical private/public text.
// It deliberately does not create a server key, a PSK, a host key file or any
// persisted identity.  The private text is handed to the native caller, which
// must put it directly into Keychain/Keystore and clear its buffer; it must
// never cross into the WebView, logs, argv, the clipboard or a state record.
//
// The six pre-existing exports in bridge.go are unchanged.  This symbol is
// additive and is the only place that generates a Tailnet key.
package main

/*
#include <stdint.h>
*/
import "C"

import (
	"bytes"
	"unsafe"

	"tailscale.com/types/key"
)

const (
	nodeKeyTextHexChars   = 64
	nodePrivateTextPrefix = "privkey:"
	nodePublicTextPrefix  = "nodekey:"
)

// tailnetKeyGenerator is the injectable seam for the pure generation path.
// Production always passes newTailnetNodeKeyText; the exported ABI never
// accepts a generator or any other caller-supplied injection point.
type tailnetKeyGenerator func() (private []byte, public []byte, err error)

// newTailnetNodeKeyText creates one app-owned outbound node key.  key.NewNode
// creates the private key in memory; MarshalText and Public().String() are the
// library-canonical "privkey:<hex>" and "nodekey:<hex>" forms.  Nothing is
// persisted here and no host key file or PSK is touched.
func newTailnetNodeKeyText() ([]byte, []byte, error) {
	private := key.NewNode()
	privateText, err := private.MarshalText()
	if err != nil {
		return nil, nil, err
	}
	return privateText, []byte(private.Public().String()), nil
}

// canonicalNodeKeyText accepts exactly the v1 textual form: the fixed prefix,
// 64 lowercase hexadecimal characters and no other bytes.
func canonicalNodeKeyText(text []byte, prefix string) bool {
	if len(text) != len(prefix)+nodeKeyTextHexChars ||
		!bytes.HasPrefix(text, []byte(prefix)) {
		return false
	}
	for _, character := range text[len(prefix):] {
		switch {
		case character >= '0' && character <= '9':
		case character >= 'a' && character <= 'f':
		default:
			return false
		}
	}
	return true
}

// clearTailnetKeyBytes is the best-effort zeroing used for the Go-side private
// text.  Go cannot promise that a key object or a copied string is erased from
// every allocation, so this is deliberately documented as best-effort rather
// than as a guarantee; the native caller must also clear its own buffer.
func clearTailnetKeyBytes(data []byte) {
	clear(data)
}

// generateTailnetKeyBuffers is the pure, testable generation path.  The two
// slices are the caller's output buffers; their lengths are the C capacities.
//
// Every failure is atomic: both capacities are checked before any byte is
// written, and on a short capacity both buffers are cleared while both lengths
// report the required (untruncated) sizes.  A generation error, invalid key
// text or panic clears both buffers and reports zero lengths.
func generateTailnetKeyBuffers(privateOut, publicOut []byte, generate tailnetKeyGenerator) (privateLen, publicLen int, code int32) {
	if len(privateOut) < 1 || len(privateOut) > MaxTailnetKeyBytes ||
		len(publicOut) < 1 || len(publicOut) > MaxTailnetKeyBytes {
		return 0, 0, ErrConfig
	}
	defer func() {
		if recover() != nil {
			clearTailnetKeyBytes(privateOut)
			clearTailnetKeyBytes(publicOut)
			privateLen, publicLen = 0, 0
			code = ErrUnavailable
		}
	}()

	private, public, err := generate()
	if private != nil {
		// This defer runs before the panic recovery defer above, so the
		// temporary private text is cleared on both success and failure.
		defer clearTailnetKeyBytes(private)
	}
	if err != nil ||
		!canonicalNodeKeyText(private, nodePrivateTextPrefix) ||
		!canonicalNodeKeyText(public, nodePublicTextPrefix) {
		clearTailnetKeyBytes(privateOut)
		clearTailnetKeyBytes(publicOut)
		return 0, 0, ErrUnavailable
	}

	requiredPrivate, requiredPublic := len(private), len(public)
	if len(privateOut) < requiredPrivate || len(publicOut) < requiredPublic {
		// Do not write either key: a caller must never see a half result.
		// The lengths still describe what a correctly sized retry needs.
		clearTailnetKeyBytes(privateOut)
		clearTailnetKeyBytes(publicOut)
		return requiredPrivate, requiredPublic, ErrResponseCapTooSmall
	}

	// Both capacities are sufficient, so only now is any output byte written.
	copy(privateOut, private)
	copy(publicOut, public)
	return requiredPrivate, requiredPublic, ErrOK
}

// generateTailnetKeyAt is the pointer/capacity layer shared by the C export
// and the unit tests.  It owns the nil and capacity checks, and it recovers a
// panic before returning across the CGO boundary.  No pointer is retained.
func generateTailnetKeyAt(
	privateOut unsafe.Pointer, privateCap int32, privateLen *int32,
	publicOut unsafe.Pointer, publicCap int32, publicLen *int32,
	generate tailnetKeyGenerator,
) (code int32) {
	if privateLen != nil {
		*privateLen = 0
	}
	if publicLen != nil {
		*publicLen = 0
	}
	if privateLen == nil || publicLen == nil || privateOut == nil || publicOut == nil ||
		privateCap < 1 || privateCap > MaxTailnetKeyBytes ||
		publicCap < 1 || publicCap > MaxTailnetKeyBytes {
		return ErrConfig
	}

	privateBuffer := unsafe.Slice((*byte)(privateOut), int(privateCap))
	publicBuffer := unsafe.Slice((*byte)(publicOut), int(publicCap))
	defer func() {
		if recover() != nil {
			clearTailnetKeyBytes(privateBuffer)
			clearTailnetKeyBytes(publicBuffer)
			*privateLen = 0
			*publicLen = 0
			code = ErrUnavailable
		}
	}()

	requiredPrivate, requiredPublic, result := generateTailnetKeyBuffers(
		privateBuffer, publicBuffer, generate,
	)
	*privateLen = int32(requiredPrivate)
	*publicLen = int32(requiredPublic)
	return result
}

//export hyprial_bridge_generate_tailnet_key
func hyprial_bridge_generate_tailnet_key(
	private_out *C.uint8_t, private_cap C.int32_t, private_len *C.int32_t,
	public_out *C.uint8_t, public_cap C.int32_t, public_len *C.int32_t,
) (code C.int32_t) {
	// Final guard: no panic may cross the CGO boundary.  The inner layer
	// already zeroes the outputs for its own panics; this guard also covers
	// any unexpected failure outside that layer.
	defer func() {
		if recover() != nil {
			if private_out != nil && private_cap >= 1 && private_cap <= MaxTailnetKeyBytes {
				clearTailnetKeyBytes(unsafe.Slice((*byte)(unsafe.Pointer(private_out)), int(private_cap)))
			}
			if public_out != nil && public_cap >= 1 && public_cap <= MaxTailnetKeyBytes {
				clearTailnetKeyBytes(unsafe.Slice((*byte)(unsafe.Pointer(public_out)), int(public_cap)))
			}
			if private_len != nil {
				*private_len = 0
			}
			if public_len != nil {
				*public_len = 0
			}
			code = C.int32_t(ErrUnavailable)
		}
	}()
	return C.int32_t(generateTailnetKeyAt(
		unsafe.Pointer(private_out), int32(private_cap), (*int32)(unsafe.Pointer(private_len)),
		unsafe.Pointer(public_out), int32(public_cap), (*int32)(unsafe.Pointer(public_len)),
		newTailnetNodeKeyText,
	))
}
