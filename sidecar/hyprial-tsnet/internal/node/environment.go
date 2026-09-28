package node

import "os"

var clearedEnvironment = []string{
	"TS_AUTHKEY", "TS_AUTH_KEY", "TS_CLIENT_SECRET",
	"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
	"http_proxy", "https_proxy", "all_proxy",
}

// ClearAmbientCredentialsAndProxy must run before hello is written. The sidecar
// accepts credentials only on stdin and a proxy only in protocol up.proxy.
func ClearAmbientCredentialsAndProxy() {
	for _, name := range clearedEnvironment {
		_ = os.Unsetenv(name)
	}
}

func SetRequestProxy(proxy string) error {
	// Clear again at the up boundary in case an embedding test/process changed
	// its environment after startup.
	ClearAmbientCredentialsAndProxy()
	if proxy == "" {
		return nil
	}
	return os.Setenv("HTTPS_PROXY", proxy)
}
