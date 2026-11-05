// Package driver defines the READ-ONLY protocol driver interface. Drivers are independent of each
// other; adding Modbus TCP later means one more implementation plus a Factory entry.
package driver

import (
	"context"
	"errors"

	"s7poc/internal/config"
	"s7poc/internal/model"
)

// ErrComm marks a transport-level failure (the whole read failed).
var ErrComm = errors.New("communication failure")

type Driver interface {
	Protocol() string // s7comm | opcua | mock-s7comm | mock-opcua
	Kind() string     // s7comm | opcua
	PollingMS() int
	Connect(ctx context.Context) error
	Close()
	Connected() bool
	// Read reads every configured tag once. Bad single items are returned with Quality INVALID.
	Read(ctx context.Context) ([]model.Reading, error)
	Browse(ctx context.Context, start string, depth int) ([]map[string]any, error)
}

// Factory builds one driver per enabled protocol.
func Factory(d config.Device) []Driver {
	var out []Driver
	if d.Mock {
		if d.S7.On() {
			out = append(out, NewMock(d, "s7comm", nil))
		}
		if d.OpcUa.On() {
			out = append(out, NewMock(d, "opcua", nil))
		}
		return out
	}
	if d.S7.On() {
		out = append(out, NewS7(d))
	}
	if d.OpcUa.On() {
		out = append(out, NewOpcUa(d))
	}
	return out
}
