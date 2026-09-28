package node

import (
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAmbientCredentialAndProxyVariablesAreCleared(t *testing.T) {
	for _, name := range clearedEnvironment {
		t.Setenv(name, "must-not-survive")
	}
	ClearAmbientCredentialsAndProxy()
	for _, name := range clearedEnvironment {
		if _, ok := os.LookupEnv(name); ok {
			t.Fatalf("%s was not cleared", name)
		}
	}
}

func TestOnlyRequestProxyIsInstalled(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://ambient.invalid")
	if err := SetRequestProxy("http://requested.invalid:8080"); err != nil {
		t.Fatal(err)
	}
	if got := os.Getenv("HTTPS_PROXY"); got != "http://requested.invalid:8080" {
		t.Fatalf("HTTPS_PROXY = %q", got)
	}
	if _, ok := os.LookupEnv("HTTP_PROXY"); ok {
		t.Fatal("HTTP_PROXY survived")
	}
}

func TestTSNetIgnoresAmbientProxyAndUsesOnlyRequestProxy(t *testing.T) {
	var targetCount atomic.Int64
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		targetCount.Add(1)
		http.Error(w, "test control response", http.StatusBadGateway)
	}))
	defer target.Close()
	var proxyCount atomic.Int64
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		proxyCount.Add(1)
		http.Error(w, "test proxy response", http.StatusBadGateway)
	}))
	defer proxy.Close()

	runProxyHelper(t, "ambient", target.URL, proxy.URL)
	if targetCount.Load() == 0 {
		t.Fatal("direct control server received no request")
	}
	if got := proxyCount.Load(); got != 0 {
		t.Fatalf("ambient proxy received %d requests, want 0", got)
	}

	targetCount.Store(0)
	proxyCount.Store(0)
	runProxyHelper(t, "request", target.URL, proxy.URL)
	if got := proxyCount.Load(); got == 0 {
		t.Fatal("up.proxy received no request")
	}
}

func TestProxySubprocess(t *testing.T) {
	mode := os.Getenv("HYPRIAL_PROXY_TEST_MODE")
	if mode == "" {
		return
	}
	controlURL := os.Getenv("HYPRIAL_PROXY_TEST_TARGET")
	switch mode {
	case "ambient":
		ClearAmbientCredentialsAndProxy()
	case "request":
		controlURL = "https://control.hyprial.invalid"
		if err := SetRequestProxy(os.Getenv("HYPRIAL_PROXY_TEST_PROXY")); err != nil {
			t.Fatal(err)
		}
	default:
		t.Fatalf("unknown helper mode %q", mode)
	}
	backend := TSNetFactory(Config{ControlURL: controlURL, Hostname: "proxy-test", Dir: t.TempDir()})
	if err := backend.Start(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Second)
	_ = backend.Close()
}

func runProxyHelper(t *testing.T, mode, target, proxy string) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestProxySubprocess$")
	env := make([]string, 0, len(os.Environ())+4)
	for _, item := range os.Environ() {
		if !strings.HasPrefix(item, "HYPRIAL_PROXY_TEST_") && !strings.HasPrefix(item, "HTTPS_PROXY=") {
			env = append(env, item)
		}
	}
	env = append(env,
		"HYPRIAL_PROXY_TEST_MODE="+mode,
		"HYPRIAL_PROXY_TEST_TARGET="+target,
		"HYPRIAL_PROXY_TEST_PROXY="+proxy,
		"HTTPS_PROXY="+proxy,
	)
	command.Env = env
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("proxy helper: %v\n%s", err, output)
	}
}
