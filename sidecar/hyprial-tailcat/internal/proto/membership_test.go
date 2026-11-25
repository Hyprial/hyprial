package proto

import (
	"fmt"
	"testing"
)

func TestTransportDiagnosticContract(t *testing.T) {
	for _, data := range []string{
		`{"stage":"derp","cause":"proxy-rejected","proxy":"configured"}`,
		`{"stage":"control","cause":"deadline-exceeded","proxy":"not-configured"}`,
	} {
		line := fmt.Sprintf(`{"v":6,"event":"error","op":"invite-redeem","requestId":"r","code":"AUTHORITY_UNREACHABLE","message":"safe","data":%s}`, data)
		if _, err := ParseEvent([]byte(line)); err != nil {
			t.Fatal(err)
		}
	}
	for _, data := range []string{
		`{"stage":"derp","cause":"secret","proxy":"configured"}`,
		`{"stage":"derp","cause":"proxy-rejected","proxy":"configured","url":"secret"}`,
		`{"stage":"derp","cause":"proxy-rejected"}`,
		`null`,
	} {
		line := fmt.Sprintf(`{"v":6,"event":"error","op":"invite-redeem","requestId":"r","code":"AUTHORITY_UNREACHABLE","message":"safe","data":%s}`, data)
		if _, err := ParseEvent([]byte(line)); err == nil {
			t.Fatal("invalid diagnostic accepted")
		}
	}
}

func TestV6MembershipContract(t *testing.T) {
	valid := []string{`{"v":6,"op":"roster-fetch","requestId":"r","params":{"tailnetId":"org-space"}}`, `{"v":6,"op":"up","requestId":"up","keyFile":"/tmp/key","addressFile":"/tmp/address","membershipStore":"/tmp/tailnet/membership.json","owner":"owner","deviceId":"device","inboundTarget":"unix:/tmp/private/zenoh.sock"}`}
	for _, line := range valid {
		if _, err := ParseCommand([]byte(line)); err != nil {
			t.Fatal(err)
		}
	}
	invalid := []string{`{"v":5,"op":"status"}`, `{"v":6,"op":"allow","keys":[],"allowAny":false}`, `{"v":6,"op":"roster-fetch","params":{"tailnetId":"org-space"}}`, `{"v":6,"op":"membership-status","requestId":"r","params":{"tailnetId":"one","tailnetId":"two"}}`, `{"v":6,"op":"expose","port":80,"target":"unix:/tmp/private/socket","proxyProtocol":"v2","admission":"any"}`}
	for _, line := range invalid {
		if _, err := ParseCommand([]byte(line)); err == nil {
			t.Fatalf("accepted %s", line)
		}
	}
}
