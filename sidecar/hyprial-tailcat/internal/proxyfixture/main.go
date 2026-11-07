// Test-only address fixture for the isolated daemon proxy-join runner.
// Preserve keys and PSK; prepend three rejected regions to the local relay.
package main

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
)

func main() {
	data, err := io.ReadAll(io.LimitReader(os.Stdin, 65537))
	if err != nil || len(data) > 65536 {
		fmt.Fprintln(os.Stderr, "invalid fixture input")
		os.Exit(1)
	}
	info, err := tailcat.ParseAddr(tailcat.Addr(strings.TrimSpace(string(data))))
	if err != nil || len(info.Region) != 1 || len(info.Region[0].Nodes) != 1 {
		fmt.Fprintln(os.Stderr, "expected single-region local relay address")
		os.Exit(1)
	}
	original := info.Region[0]
	if original.Nodes[0].HostName != "relay.invalid" {
		fmt.Fprintln(os.Stderr, "fixture requires relay.invalid")
		os.Exit(1)
	}
	info.Region = nil
	for i, host := range []string{"blocked.invalid", "also-blocked.invalid", "third-blocked.invalid", "relay.invalid"} {
		region := *original
		region.RegionID = tailcfg.DERPRegionID(i + 1)
		region.RegionCode = fmt.Sprintf("fixture-%d", i+1)
		node := *original.Nodes[0]
		node.Name, node.HostName, node.RegionID = host, host, region.RegionID
		region.Nodes = []*tailcfg.DERPNode{&node}
		info.Region = append(info.Region, &region)
	}
	fmt.Println(info.Addr())
}
