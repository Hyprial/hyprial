// Package keys manages the hyprial-tailcat device key file (protocol v3,
// design doc §3.2). The file holds separate inbound server and outbound
// client node keys plus the WireGuard pre-shared key; its contents are
// secret and must never reach argv, stdout events, or logs.
package keys

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"github.com/tailscale/tailcat"
	"tailscale.com/types/key"
)

// ErrExists is returned by Generate when the key file already exists and
// force was not requested. The CLI maps it to exit code 3.
var ErrExists = errors.New("key file already exists")

// ErrInvalid is returned by Load for missing, unreadable, or malformed
// key files. The serve layer maps it to the KEY_FILE_INVALID error code.
// Load error text never embeds key material.
var ErrInvalid = errors.New("key file invalid")

// File is the on-disk key file format (design doc §3.2).
type File struct {
	V      int `json:"v"`
	Server struct {
		Private      string `json:"private"`
		PresharedKey string `json:"presharedKey"`
	} `json:"server"`
	Client struct {
		Private string `json:"private"`
	} `json:"client"`
}

// Material is the parsed, usable form of a key file.
type Material struct {
	ServerPrivate key.NodePrivate
	PresharedKey  tailcat.PresharedKey
	ClientPrivate key.NodePrivate
}

// ServerPublic returns the inbound identity announced to clients.
func (m Material) ServerPublic() key.NodePublic { return m.ServerPrivate.Public() }

// ClientPublic returns the outbound identity servers allowlist.
func (m Material) ClientPublic() key.NodePublic { return m.ClientPrivate.Public() }

// PublicKeys is the only data genkey may print: the two public keys.
type PublicKeys struct {
	ServerPublic string
	ClientPublic string
}

// Generate writes a fresh key file at path with mode 0600. It refuses to
// overwrite an existing file unless force is set; without force an
// existing file yields ErrExists. The returned PublicKeys are safe to
// print; private keys and the PSK stay in the file.
func Generate(path string, force bool) (PublicKeys, error) {
	material := Material{
		ServerPrivate: key.NewNode(),
		PresharedKey:  tailcat.NewPresharedKey(),
		ClientPrivate: key.NewNode(),
	}
	pskText, err := material.PresharedKey.MarshalText()
	if err != nil {
		return PublicKeys{}, fmt.Errorf("encode preshared key: %w", err)
	}
	file := File{V: 3}
	serverText, err := material.ServerPrivate.MarshalText()
	if err != nil {
		return PublicKeys{}, fmt.Errorf("encode server private key: %w", err)
	}
	clientText, err := material.ClientPrivate.MarshalText()
	if err != nil {
		return PublicKeys{}, fmt.Errorf("encode client private key: %w", err)
	}
	file.Server.Private = string(serverText)
	file.Server.PresharedKey = string(pskText)
	file.Client.Private = string(clientText)
	encoded, err := json.Marshal(file)
	if err != nil {
		return PublicKeys{}, fmt.Errorf("encode key file: %w", err)
	}
	flag := os.O_WRONLY | os.O_CREATE | os.O_EXCL
	if force {
		flag = os.O_WRONLY | os.O_CREATE | os.O_TRUNC
	}
	handle, err := os.OpenFile(path, flag, 0600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return PublicKeys{}, ErrExists
		}
		return PublicKeys{}, fmt.Errorf("create key file: %w", err)
	}
	if _, err := handle.Write(append(encoded, '\n')); err != nil {
		_ = handle.Close()
		return PublicKeys{}, fmt.Errorf("write key file: %w", err)
	}
	if err := handle.Close(); err != nil {
		return PublicKeys{}, fmt.Errorf("close key file: %w", err)
	}
	// Re-assert permissions so --force on a pre-existing file cannot keep
	// a wider mode.
	if err := os.Chmod(path, 0600); err != nil {
		return PublicKeys{}, fmt.Errorf("chmod key file: %w", err)
	}
	return PublicKeys{
		ServerPublic: material.ServerPublic().String(),
		ClientPublic: material.ClientPublic().String(),
	}, nil
}

// Load reads and validates a key file.
func Load(path string) (Material, error) {
	encoded, err := os.ReadFile(path)
	if err != nil {
		return Material{}, fmt.Errorf("%w: unreadable", ErrInvalid)
	}
	var file File
	if err := json.Unmarshal(encoded, &file); err != nil {
		return Material{}, fmt.Errorf("%w: not a JSON key file", ErrInvalid)
	}
	if file.V != 3 {
		return Material{}, fmt.Errorf("%w: unsupported key file version", ErrInvalid)
	}
	var server key.NodePrivate
	if err := server.UnmarshalText([]byte(file.Server.Private)); err != nil {
		return Material{}, fmt.Errorf("%w: bad server private key", ErrInvalid)
	}
	var psk tailcat.PresharedKey
	if err := psk.UnmarshalText([]byte(file.Server.PresharedKey)); err != nil {
		return Material{}, fmt.Errorf("%w: bad preshared key", ErrInvalid)
	}
	var client key.NodePrivate
	if err := client.UnmarshalText([]byte(file.Client.Private)); err != nil {
		return Material{}, fmt.Errorf("%w: bad client private key", ErrInvalid)
	}
	return Material{ServerPrivate: server, PresharedKey: psk, ClientPrivate: client}, nil
}
