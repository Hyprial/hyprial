package servicehost

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "host.json")
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func validConfigJSON() string {
	return `{
  "keyFile": "/var/lib/hyprial-tailcat/host.key",
  "addressFile": "/var/lib/hyprial-tailcat/host.address",
  "clientPublic": [],
  "allowAny": false,
  "exposures": [{"port": 18080, "target": "tcp:127.0.0.1:8080"}]
}`
}

func TestLoadConfigStrictAndDefaultsProxyProtocol(t *testing.T) {
	cfg, err := LoadConfig(writeConfig(t, validConfigJSON()))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if cfg.AllowAny || len(cfg.ClientPublic) != 0 || len(cfg.Exposures) != 1 {
		t.Fatalf("config = %+v", cfg)
	}
	if got := cfg.Exposures[0].ProxyProtocol; got != "" {
		t.Fatalf("proxy protocol = %q, want empty default", got)
	}
}

func TestLoadConfigRejectsUnsafeValues(t *testing.T) {
	cases := []struct {
		name string
		body string
		want string
	}{
		{"unknown field", strings.Replace(validConfigJSON(), "\n}", ",\n  \"secret\": \"no\"\n}", 1), "unknown field"},
		{"allow any", strings.Replace(validConfigJSON(), "\"allowAny\": false", "\"allowAny\": true", 1), "allowAny"},
		{"non-loopback target", strings.Replace(validConfigJSON(), "127.0.0.1", "0.0.0.0", 1), "loopback"},
		{"bad proxy", strings.Replace(validConfigJSON(), "\"target\": \"tcp:127.0.0.1:8080\"", "\"target\": \"tcp:127.0.0.1:8080\", \"proxyProtocol\": \"http\"", 1), "proxyProtocol"},
		{"relative key", strings.Replace(validConfigJSON(), "/var/lib/hyprial-tailcat/host.key", "host.key", 1), "absolute"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := LoadConfig(writeConfig(t, tc.body))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want substring %q", err, tc.want)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidAllowKeyAndDuplicatePorts(t *testing.T) {
	badKey := strings.Replace(validConfigJSON(), `"clientPublic": []`, `"clientPublic": ["not-a-key"]`, 1)
	if _, err := LoadConfig(writeConfig(t, badKey)); err == nil {
		t.Fatal("invalid clientPublic key was accepted")
	}
	duplicate := strings.Replace(validConfigJSON(), `{"port": 18080, "target": "tcp:127.0.0.1:8080"}`, `{"port": 18080, "target": "tcp:127.0.0.1:8080"}, {"port": 18080, "target": "unix:/tmp/app.sock"}`, 1)
	if _, err := LoadConfig(writeConfig(t, duplicate)); err == nil {
		t.Fatal("duplicate exposure port was accepted")
	}
}
