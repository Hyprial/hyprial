package membershiptest

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"code.hyprial.com/HyprialOS/harness-bridge/sidecar/hyprial-tailcat/internal/membership"
)

func TestReviewProxyRegionHint(t *testing.T) {
	proxyFallback(t, "region-hint")
}

func proxyRejections(t *testing.T) int64 {
	t.Helper()
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get(os.Getenv("HYPRIAL_REVIEW_COUNTER_URL") + "/rejections")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 64))
	if err != nil {
		t.Fatal(err)
	}
	value, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil || response.StatusCode != http.StatusOK {
		t.Fatal("invalid proxy counter response")
	}
	return value
}

func threeHintedExchanges(t *testing.T, a, b *node) {
	t.Helper()
	initNode(t, a, a, "fallback-tn", []membership.Member{a.row, b.row})
	initNode(t, b, a, "fallback-tn", []membership.Member{})
	var first int64
	for i := range 3 {
		started := time.Now()
		if len(fetch(t, b, "fallback-tn").Members) != 2 {
			t.Fatal("missing membership")
		}
		rejected := proxyRejections(t)
		fmt.Printf("exchange hint=%d elapsed=%s rejections=%d\n", i+1, time.Since(started), rejected)
		if i == 0 {
			first = rejected
			if first == 0 {
				t.Fatal("first exchange did not attempt rejected regions")
			}
		} else if rejected != first {
			t.Fatalf("exchange %d retried rejected regions: before=%d after=%d", i+1, first, rejected)
		}
	}
}
