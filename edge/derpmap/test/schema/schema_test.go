package derpmapschema

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"tailscale.com/tailcfg"
)

func TestMergedDerpMapParses(t *testing.T) {
	raw, err := os.ReadFile("merged.json")
	if err != nil {
		t.Fatalf("read merged.json: %v", err)
	}
	var m tailcfg.DERPMap
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("merged output does not parse as tailcfg.DERPMap: %v", err)
	}
	region, ok := m.Regions[900]
	if !ok || region == nil {
		t.Fatal("region 900 missing")
	}
	if len(region.Nodes) != 1 {
		t.Fatalf("region 900 has %d nodes", len(region.Nodes))
	}
	node := region.Nodes[0]
	if node.HostName != "6ad6d7fbc3f1e.cdnyou.com" || node.DERPPort != 1279 {
		t.Fatalf("unexpected node %+v", node)
	}
	if !strings.HasPrefix(node.CertName, "sha256-raw:") {
		t.Fatalf("CertName %q is not a sha256-raw pin", node.CertName)
	}
	if node.InsecureForTests {
		t.Fatal("InsecureForTests must stay false")
	}
}
