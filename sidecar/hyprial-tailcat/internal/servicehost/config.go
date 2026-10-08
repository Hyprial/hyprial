// Package servicehost runs the generic Docker service-host lifecycle.
// Configuration is operator-owned and deliberately contains no private
// Tailcat address: the address is produced by Engine.Up in addressFile.
package servicehost

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var nodePublic = regexp.MustCompile(`^nodekey:[0-9a-f]{64}$`)

// Config is the strict, non-secret host configuration. allowAny is retained
// as an explicit field so a typo cannot silently widen admission.
type Config struct {
	KeyFile      string     `json:"keyFile"`
	AddressFile  string     `json:"addressFile"`
	ClientPublic []string   `json:"clientPublic"`
	AllowAny     bool       `json:"allowAny"`
	Exposures    []Exposure `json:"exposures"`
}

// Exposure is one Tailcat listener and its loopback application target.
type Exposure struct {
	Port          int    `json:"port"`
	Target        string `json:"target"`
	ProxyProtocol string `json:"proxyProtocol,omitempty"`
}

// LoadConfig parses and validates an operator config before any engine state
// is changed. It rejects unknown fields and preserves deny-all semantics.
func LoadConfig(path string) (Config, error) {
	if !filepath.IsAbs(path) {
		return Config{}, fmt.Errorf("config path must be absolute")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read config: %w", err)
	}
	var raw map[string]json.RawMessage
	if err := decodeOne(data, &raw); err != nil {
		return Config{}, fmt.Errorf("invalid config: %w", err)
	}
	for _, key := range []string{"keyFile", "addressFile", "clientPublic", "allowAny", "exposures"} {
		if _, ok := raw[key]; !ok {
			return Config{}, fmt.Errorf("config missing %q", key)
		}
	}
	for _, key := range []string{"clientPublic", "allowAny", "exposures"} {
		if bytes.Equal(bytes.TrimSpace(raw[key]), []byte("null")) {
			return Config{}, fmt.Errorf("config %q has invalid null value", key)
		}
	}
	var cfg Config
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return Config{}, fmt.Errorf("invalid config: %w", err)
	}
	var rawExposures []map[string]json.RawMessage
	if err := json.Unmarshal(raw["exposures"], &rawExposures); err != nil {
		return Config{}, fmt.Errorf("exposures must be an array")
	}
	for _, exposure := range rawExposures {
		if value, ok := exposure["proxyProtocol"]; ok && bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return Config{}, fmt.Errorf("proxyProtocol must be a string")
		}
	}
	if err := validateConfig(cfg); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func decodeOne(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err == nil {
		return fmt.Errorf("trailing JSON data")
	} else if !errors.Is(err, io.EOF) {
		return fmt.Errorf("trailing JSON data")
	}
	return nil
}

func validateConfig(cfg Config) error {
	if !filepath.IsAbs(cfg.KeyFile) || !filepath.IsAbs(cfg.AddressFile) {
		return fmt.Errorf("keyFile and addressFile must be absolute paths")
	}
	if cfg.AllowAny {
		return fmt.Errorf("allowAny must be false")
	}
	for _, public := range cfg.ClientPublic {
		if !nodePublic.MatchString(public) {
			return fmt.Errorf("clientPublic contains an invalid nodekey")
		}
	}
	seen := make(map[int]struct{}, len(cfg.Exposures))
	for _, exposure := range cfg.Exposures {
		if _, ok := seen[exposure.Port]; ok {
			return fmt.Errorf("duplicate exposure port %d", exposure.Port)
		}
		seen[exposure.Port] = struct{}{}
		if err := validateExposure(exposure); err != nil {
			return err
		}
	}
	return nil
}

func validateExposure(exposure Exposure) error {
	if exposure.Port < 1 || exposure.Port > 65535 {
		return fmt.Errorf("exposure port must be 1..65535")
	}
	if exposure.ProxyProtocol != "" && exposure.ProxyProtocol != "v2" {
		return fmt.Errorf("proxyProtocol must be empty or v2")
	}
	switch {
	case strings.HasPrefix(exposure.Target, "tcp:"):
		host, port, err := net.SplitHostPort(strings.TrimPrefix(exposure.Target, "tcp:"))
		if err != nil || host != "127.0.0.1" || port == "" {
			return fmt.Errorf("exposure target must use loopback tcp")
		}
		number, err := strconv.Atoi(port)
		if err != nil || number < 1 || number > 65535 || strconv.Itoa(number) != port {
			return fmt.Errorf("exposure target must use a valid TCP port")
		}
	case strings.HasPrefix(exposure.Target, "unix:"):
		if !filepath.IsAbs(strings.TrimPrefix(exposure.Target, "unix:")) {
			return fmt.Errorf("exposure unix target must be absolute")
		}
	default:
		return fmt.Errorf("exposure target must be tcp loopback or unix absolute path")
	}
	return nil
}
