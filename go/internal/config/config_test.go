package config

import "testing"

const y = `
devices:
  - id: a-1
    ip: 1.2.3.4
    s7comm: {rack: 0}
    tags: [{name: T, type: REAL, s7_address: DB1.DBD0}]
`

func TestDefaultsAndEnvSecrets(t *testing.T) {
	t.Setenv("OPCUA_USERNAME", "u")
	t.Setenv("OPCUA_PASSWORD", "s3cret")
	c, err := Parse([]byte(y), false)
	if err != nil {
		t.Fatal(err)
	}
	d := c.Devices[0]
	if *d.S7.Slot != 1 || d.S7.Port != 102 || d.S7.PollingMS != 1000 || !d.S7.On() || !d.OpcUa.On() {
		t.Fatalf("defaults wrong: %+v", d.S7)
	}
	if d.OpcUa.Username != "u" || d.OpcUa.Password != "s3cret" {
		t.Fatal("env creds not loaded")
	}
}

func TestRequiresTags(t *testing.T) {
	if _, err := Parse([]byte("devices: [{id: a, ip: x}]"), false); err == nil {
		t.Fatal("expected error")
	}
}
